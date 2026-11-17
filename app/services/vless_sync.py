"""Incremental, fenced RemnaWave 3.4.4 observations. Never writes remote users.

Paid terms remain authoritative locally. Only confirmed usage/connection URLs
are cached on credentials; status, limits and expiry are observed separately.
"""

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from cryptography.fernet import InvalidToken
from sqlalchemy import exists, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.database.models import (
    AccessKey,
    RemnawaveServer,
    Subscription,
    VlessCreation,
    VlessCredential,
    VlessRenewal,
    VlessReservation,
    VlessSync,
    VlessSyncGate,
    now,
)
from app.database.repositories import SQLRepository
from app.services import vless_capacity
from app.services.remnawave import RemnawaveApiError, RemnawaveService
from app.services.vless_creation import confirmed, utc
from app.services.vless_renewal import PENDING

BATCH_SIZE = 32
LEASE_SECONDS = 60  # A single GET has a 15 second provider timeout.
REQUEST_SECONDS = 20  # Also bound custom clients and stalled response handling.
INTERVAL_SECONDS = 300
RATE_SECONDS = 0.25  # At most four reconciliation GETs/sec/panel across replicas.
logger = logging.getLogger(__name__)
VERIFIED = {"confirmed", "missing", "creation_conflict"}


class SourceChanged(ValueError):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def date(value):
    if not isinstance(value, str):
        raise ValueError("Missing/invalid remote date")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Remote date has no timezone")
    return utc(result)


def counter(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("Missing/invalid remote counter")
    return value


def observation(payload, identity=None):
    """Validate the installed numeric-ID contract; tolerate legacy UUID keys."""
    if not isinstance(payload, dict):
        raise ValueError("Invalid user response")
    data = payload.get("response", payload)
    if not isinstance(data, dict):
        raise ValueError("Invalid user response")
    remote_id = data.get("id", data.get("uuid"))
    if identity is not None and str(remote_id) != str(identity):
        raise ValueError("Remote identity mismatch")
    status = data.get("status")
    if status not in {"ACTIVE", "DISABLED", "EXPIRED", "LIMITED"}:
        raise ValueError("Invalid remote status")
    limit = data["hwidDeviceLimit"]  # null means unspecified, never zero.
    if limit is not None:
        counter(limit)
    url = data["subscriptionUrl"]
    if not isinstance(url, str):
        raise ValueError("Invalid subscription URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or any(c.isspace() for c in url)
    ):
        raise ValueError("Invalid subscription URL")
    short = data["shortUuid"]
    if not isinstance(short, str) or not short:
        raise ValueError("Invalid short UUID")
    if date(data["updatedAt"]) > now() + timedelta(minutes=5):
        raise ValueError("Remote update timestamp is in the future")
    traffic = data["userTraffic"]
    reset = data["lastTrafficResetAt"]
    online = traffic["onlineAt"]
    return dict(
        exists=True,
        identity=str(remote_id),
        status=status,
        expires_at=date(data["expireAt"]).isoformat(),
        device_limit=limit,
        traffic_limit=counter(data["trafficLimitBytes"]),
        traffic_used=counter(traffic["usedTrafficBytes"]),
        lifetime_traffic_used=counter(traffic["lifetimeUsedTrafficBytes"]),
        subscription_url=url,
        short_uuid=short,
        updated_at=date(data["updatedAt"]).isoformat(),
        traffic_reset_at=date(reset).isoformat() if reset is not None else None,
        online_at=date(online).isoformat() if online is not None else None,
    )


def stale(previous, incoming):
    if not previous or "updated_at" not in previous:
        return False
    if date(incoming["updated_at"]) < date(previous["updated_at"]):
        return True
    # Traffic changes don't always change user.updatedAt. A decrease requires
    # a confirmed later reset; an older traffic sample must not erase usage.
    reset = incoming["traffic_reset_at"]
    old_reset = previous["traffic_reset_at"]
    reset_advanced = reset and (not old_reset or date(reset) > date(old_reset))
    return bool(
        incoming["lifetime_traffic_used"] < previous["lifetime_traffic_used"]
        or (incoming["traffic_used"] < previous["traffic_used"] and not reset_advanced)
    )


async def local_state(session, credential, panel):
    sub = await session.get(Subscription, credential.subscription_id)
    key = await session.get(AccessKey, credential.access_key_id)
    pending = await session.scalar(
        select(VlessRenewal).where(
            VlessRenewal.subscription_id == credential.subscription_id,
            VlessRenewal.state.in_(PENDING),
        )
    )
    creation = await session.scalar(
        select(VlessCreation).where(
            VlessCreation.subscription_id == credential.subscription_id,
            VlessCreation.credential_id == credential.id,
        )
    )
    return dict(
        server_url=panel.api_url,
        server_id=credential.server_id,
        user_id=credential.user_id,
        subscription_id=credential.subscription_id,
        access_key_id=credential.access_key_id,
        identity=credential.remote_user_uuid,
        short_uuid=credential.short_uuid,
        status=credential.status,
        expires_at=utc(credential.expires_at).isoformat(),
        device_limit=credential.device_limit,
        traffic_limit=credential.traffic_limit,
        cached_traffic_used=credential.traffic_used,
        last_sync=utc(credential.last_sync).isoformat()
        if credential.last_sync
        else None,
        subscription_url=credential.subscription_url,
        subscription=(
            dict(
                expires_at=utc(sub.expires_at).isoformat(),
                device_limit=sub.device_limit,
            )
            if sub
            else None
        ),
        key=dict(status=key.status, expires_at=utc(key.expires_at).isoformat())
        if key
        else None,
        pending_renewal=(
            dict(
                id=pending.id,
                state=pending.state,
                updated_at=utc(pending.updated_at).isoformat(),
                expires_at=utc(pending.expires_at).isoformat(),
                device_limit=pending.device_limit,
                status=pending.target_status,
            )
            if pending
            else None
        ),
        pending_creation=creation.id
        if creation and creation.state != "associated"
        else None,
    )


def differences(desired, remote):
    flags = []
    if not remote["exists"]:
        return ["missing"]
    for field in ("expires_at", "device_limit", "traffic_limit"):
        different = (
            abs((date(remote[field]) - date(desired[field])).total_seconds()) >= 1
            if field == "expires_at"
            else remote[field] != desired[field]
        )
        if different:
            flags.append(field)
    status = "ACTIVE" if desired["status"] == "active" else "DISABLED"
    if remote["status"] != status:
        flags.append("status")
    if desired.get("pending_renewal"):
        flags.append("pending_renewal")
    if desired.get("pending_creation"):
        flags.append("pending_creation")
    return flags


@dataclass
class Work:
    id: str
    token: str
    panel: RemnawaveServer
    desired: dict
    credential_id: int | None
    creation: VlessCreation | None = None


class VlessReconciler:
    def __init__(self, database, settings, *, rate_seconds=RATE_SECONDS):
        self.database, self.settings = database, settings
        self.rate_seconds = rate_seconds

    async def seed(self, limit=BATCH_SIZE):
        """Bounded anti-joins seed old and newly committed keys without HTTP."""
        limit = max(1, min(limit, BATCH_SIZE))
        async with self.database.sessions.begin() as session:
            if session.bind.dialect.name != "postgresql":
                return
            # Re-arm old observations (including retired rows) after a proven
            # new-key fallback. Never reuse one panel's observations on another.
            moved = await session.execute(
                select(VlessSync, VlessCreation)
                .join(VlessCreation, VlessCreation.id == VlessSync.creation_id)
                .where(
                    VlessSync.server_id != VlessCreation.server_id,
                    VlessCreation.credential_id.is_(None),
                    VlessCreation.state.in_(("outcome_unknown", "remote_confirmed")),
                )
                .order_by(VlessSync.id)
                .with_for_update(of=VlessSync, skip_locked=True)
                .limit(limit)
            )
            for row, operation in moved:
                row.server_id = operation.server_id
                row.desired, row.observed, row.drift = "{}", None, "[]"
                row.remote_updated_at = row.last_success_at = None
                row.lease_token, row.lease_until = "", None
                row.outcome, row.next_attempt_at = "pending", now()
                row.failures, row.last_error = 0, ""
                await session.execute(
                    insert(VlessSyncGate)
                    .values(server_id=row.server_id)
                    .on_conflict_do_nothing()
                )
            for model, field, prefix, condition in (
                (
                    VlessCredential,
                    "credential_id",
                    "key:",
                    VlessCredential.status != "deleted",
                ),
                (
                    VlessCreation,
                    "creation_id",
                    "creation:",
                    (VlessCreation.credential_id.is_(None))
                    & VlessCreation.state.in_(("outcome_unknown", "remote_confirmed")),
                ),
            ):
                column = getattr(VlessSync, field)
                rows = await session.execute(
                    select(model.id, model.server_id)
                    .where(condition, ~exists(select(column).where(column == model.id)))
                    .order_by(model.id)
                    .limit(limit)
                )
                for identity, server_id in rows:
                    await session.execute(
                        insert(VlessSync)
                        .values(
                            id=prefix + str(identity),
                            server_id=server_id,
                            **{field: identity},
                        )
                        .on_conflict_do_nothing()
                    )
                    await session.execute(
                        insert(VlessSyncGate)
                        .values(server_id=server_id)
                        .on_conflict_do_nothing()
                    )

    async def claim(self, identity=None):
        async with self.database.sessions.begin() as session:
            due = [
                VlessSync.next_attempt_at <= now(),
                or_(VlessSync.lease_until.is_(None), VlessSync.lease_until <= now()),
                VlessSync.outcome != "retired",
            ]
            if identity:
                due.append(VlessSync.id == identity)
            # Gate first, then queue; SKIP LOCKED makes multiple workers safe.
            gate = await session.scalar(
                select(VlessSyncGate)
                .where(
                    VlessSyncGate.next_request_at <= now(),
                    or_(
                        VlessSyncGate.lease_until.is_(None),
                        VlessSyncGate.lease_until <= now(),
                    ),
                    exists(
                        select(VlessSync.id).where(
                            *due, VlessSync.server_id == VlessSyncGate.server_id
                        )
                    ),
                )
                .order_by(VlessSyncGate.next_request_at, VlessSyncGate.server_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if gate is None:
                return None
            row = await session.scalar(
                select(VlessSync)
                .where(*due, VlessSync.server_id == gate.server_id)
                .order_by(VlessSync.next_attempt_at, VlessSync.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            panel = await session.get(RemnawaveServer, row.server_id)
            credential = (
                await session.get(VlessCredential, row.credential_id)
                if row.credential_id
                else None
            )
            creation = (
                await session.get(VlessCreation, row.creation_id)
                if row.creation_id
                else None
            )
            if (
                panel is None
                or row.credential_id
                and (credential is None or credential.status == "deleted")
                or row.creation_id
                and (
                    creation is None
                    or creation.credential_id
                    or creation.state == "failed"
                )
            ):
                row.outcome = "retired"
                return None
            if credential and credential.server_id != row.server_id:
                row.server_id = credential.server_id
                await session.execute(
                    insert(VlessSyncGate)
                    .values(server_id=row.server_id)
                    .on_conflict_do_nothing()
                )
                return None
            desired = (
                await local_state(session, credential, panel)
                if credential
                else dict(
                    server_url=creation.server_url,
                    request=json.loads(creation.request),
                    pending_creation=creation.id,
                )
            )
            token = secrets.token_hex(16)
            row.lease_token = gate.lease_token = token
            row.lease_until = gate.lease_until = now() + timedelta(
                seconds=LEASE_SECONDS
            )
            row.last_attempt_at = now()
            row.desired = encode(desired)
            return Work(row.id, token, panel, desired, row.credential_id, creation)

    async def fetch(self, work):
        # No database transaction/row lock is open during the external GET.
        async with self.database.sessions() as session:
            try:
                api = RemnawaveService(SQLRepository(session), self.settings).api(
                    work.panel
                )
            except (InvalidToken, ValueError, KeyError) as error:
                raise RemnawaveApiError(
                    "Panel API configuration unavailable"
                ) from error
        if work.panel.api_url != work.desired["server_url"]:
            raise SourceChanged("Provisioning panel address changed")
        if work.creation:
            return await api.user_by_username(work.desired["request"]["username"])
        return await api.user(work.desired["identity"])

    async def recover_creation(self, work, payload):
        """Reuse I01/I03 confirmation, never POST or associate/activate an order."""
        op = work.creation
        result = confirmed(payload, work.desired["request"])
        async with self.database.sessions() as session:
            # The queue may have been claimed before the checkout committed.
            current = await session.get(VlessCreation, op.id)
            reservation = await session.get(VlessReservation, op.id)
            if (
                not current
                or current.credential_id
                or not reservation
                or reservation.state == "released"
                or current.server_id != op.server_id
                or current.server_url != op.server_url
                or current.request != op.request
            ):
                return
            repo = SQLRepository(session)
            claim = await vless_capacity.claim(
                repo,
                op.id,
                op.customer_ref,
                op.user_id,
                op.server_id,
                op.server_url,
                work.desired["request"],
            )
            if claim:
                # The validated stable-username GET already confirmed ownership
                # and the frozen I01 terms. The dispatch lease excludes an active
                # creator; confirmation consumes the existing reservation once.
                try:
                    await vless_capacity.finish(repo, op.id, claim, result=result)
                except SQLAlchemyError:
                    try:
                        await vless_capacity.clear_failed_commit_lease(
                            repo, op.id, claim
                        )
                    except SQLAlchemyError:
                        pass  # Durable I03 lease still expires; capacity is retained.
                    raise

    async def finish(self, work, outcome, remote=None, error=""):
        # Panel statistics now commit concurrently. Give only that short row
        # update a bounded chance to finish, without repeating the remote GET
        # or retaining any database connection/lock between attempts.
        for attempt in range(5):
            result = await self._finish(
                work, outcome, remote, error, retry_panel=attempt < 4
            )
            if result is not None:
                return result
            await asyncio.sleep(0.1)

    async def _finish(self, work, outcome, remote, error, *, retry_panel):
        async with self.database.sessions.begin() as session:
            gate = await session.get(VlessSyncGate, work.panel.id, with_for_update=True)
            row = await session.get(VlessSync, work.id, with_for_update=True)
            if (
                not gate
                or not row
                or row.lease_token != work.token
                or gate.lease_token != work.token
            ):
                return False  # An expired/crashed worker cannot overwrite a successor.
            if remote is not None and outcome in {"confirmed", "creation_conflict"}:
                previous = json.loads(row.observed) if row.observed else None
                if (
                    row.remote_updated_at
                    and date(remote["updated_at"]) < utc(row.remote_updated_at)
                ) or stale(previous, remote):
                    outcome, remote = "stale", None
            current = work.desired
            if work.creation:
                creation = await session.get(VlessCreation, work.creation.id)
                if (
                    not creation
                    or creation.server_id != work.panel.id
                    or creation.server_url != work.desired["server_url"]
                    or json.loads(creation.request) != work.desired["request"]
                ):
                    outcome, remote = "local_changed", None
            credential = None
            if work.credential_id:
                # I02 holds the subscription while applying a paid renewal.
                # Never wait on it while monopolizing sync/gate locks.
                sub_id = await session.scalar(
                    select(VlessCredential.subscription_id).where(
                        VlessCredential.id == work.credential_id
                    )
                )
                sub = await session.scalar(
                    select(Subscription)
                    .where(Subscription.id == sub_id)
                    .with_for_update(skip_locked=True)
                )
                credential = await session.scalar(
                    select(VlessCredential)
                    .where(VlessCredential.id == work.credential_id)
                    .with_for_update(skip_locked=True)
                )
                key = (
                    await session.scalar(
                        select(AccessKey)
                        .where(AccessKey.id == credential.access_key_id)
                        .with_for_update(skip_locked=True)
                    )
                    if credential
                    else None
                )
                panel = await session.scalar(
                    select(RemnawaveServer)
                    .where(RemnawaveServer.id == work.panel.id)
                    .with_for_update(skip_locked=True)
                )
                if (
                    retry_panel
                    and sub
                    and credential
                    and key
                    and not panel
                    and outcome in {"confirmed", "missing"}
                ):
                    return None  # Exit the short transaction before waiting.
                if not sub or not credential or not key or not panel:
                    if outcome in {"confirmed", "missing"}:
                        outcome = "local_busy"
                    credential = None
                else:
                    current = await local_state(session, credential, panel)
                    row.desired = encode(current)
                    if current != work.desired:
                        if outcome in {"confirmed", "missing"}:
                            outcome = "local_changed"
                        credential = None
            if remote is not None and outcome in VERIFIED:
                if outcome == "missing" and row.observed:
                    remote = {**json.loads(row.observed), **remote}
                row.observed = encode(remote)
                row.last_success_at = now()
                row.drift = encode(
                    differences(current, remote)
                    if work.credential_id
                    else ["pending_creation"]
                    + (["creation_terms"] if outcome == "creation_conflict" else [])
                )
                if outcome != "missing":
                    row.remote_updated_at = date(remote["updated_at"])
                    if (
                        outcome == "confirmed"
                        and credential
                        and not current["pending_renewal"]
                        and not current["pending_creation"]
                    ):
                        credential.traffic_used = remote["traffic_used"]
                        credential.subscription_url = remote["subscription_url"]
                        credential.short_uuid = remote["short_uuid"]
                        credential.last_sync = row.last_success_at
            row.outcome = outcome
            row.last_error = error[:255]
            row.failures = 0 if outcome in VERIFIED else min(row.failures + 1, 10)
            delay = (
                INTERVAL_SECONDS
                if outcome == "confirmed"
                else 60
                if outcome in {"missing", "creation_conflict"}
                else min(900, 15 * 2 ** (row.failures - 1))
            )
            row.next_attempt_at = now() + timedelta(seconds=delay)
            row.lease_token = gate.lease_token = ""
            row.lease_until = gate.lease_until = None
            gate.next_request_at = now() + timedelta(seconds=self.rate_seconds)
            return True

    async def process(self, work):
        outcome, remote, error = "unavailable", None, ""
        try:
            payload = await asyncio.wait_for(self.fetch(work), REQUEST_SECONDS)
            remote = observation(payload, work.desired.get("identity"))
            outcome = "confirmed"
            if work.creation:
                try:
                    await self.recover_creation(work, payload)
                except ValueError:
                    outcome, error = (
                        "creation_conflict",
                        "Remote creation differs from frozen I01 terms",
                    )
        except TimeoutError:
            outcome, remote, error = "unavailable", None, "RemnaWave request timed out"
        except SourceChanged:
            outcome, remote, error = (
                "local_changed",
                None,
                "Provisioning panel address changed",
            )
        except RemnawaveApiError as exc:
            outcome = (
                "missing"
                if exc.status == 404 and exc.error_code in {"A025", "A063"}
                else "malformed"
                if exc.status in {200, 201, 204}
                else "unavailable"
            )
            remote = dict(exists=False) if outcome == "missing" else None
            error = (
                f"RemnaWave HTTP {exc.status}"  # No secrets/URLs/raw payloads in logs.
            )
        except (ValueError, TypeError, KeyError, OverflowError):
            outcome, remote, error = (
                "malformed",
                None,
                "Invalid RemnaWave user response",
            )
        await self.finish(work, outcome, remote, error)

    async def once(self, limit=BATCH_SIZE):
        if self.database.engine.dialect.name != "postgresql":
            return
        limit = max(1, min(limit, BATCH_SIZE))
        try:
            await self.seed(limit)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("VLESS reconciliation seed failed")
            return

        async def consume(count):
            for _ in range(count):
                work = None
                try:
                    work = await self.claim()
                    if work is None:
                        break
                    await self.process(work)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Failed local commits leave a durable expiring lease.
                    # One damaged key must not prevent reconciliation of others.
                    logger.exception(
                        "VLESS reconciliation failed: %s", work.id if work else "claim"
                    )
                    if work is None:
                        break  # Retry a failed claim on the next worker pass.
                await asyncio.sleep(self.rate_seconds)

        # Await/cancel both existing consumers together, including claim failures
        # and shutdown; gather can otherwise leave a sibling running on error.
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(consume((limit + 1) // 2))
            tasks.create_task(consume(limit // 2))

    async def refresh(self, credential_id):
        """Manual refresh uses the same validation, gate, lease and caches."""
        identity = f"key:{credential_id}"
        async with self.database.sessions.begin() as session:
            credential = await session.get(VlessCredential, credential_id)
            if not credential or credential.status == "deleted":
                raise ValueError("Ключ недоступен для обновления.")
            await session.execute(
                insert(VlessSync)
                .values(
                    id=identity,
                    server_id=credential.server_id,
                    credential_id=credential_id,
                )
                .on_conflict_do_nothing()
            )
            await session.execute(
                insert(VlessSyncGate)
                .values(server_id=credential.server_id)
                .on_conflict_do_nothing()
            )
            row = await session.get(VlessSync, identity)
            if row:
                row.next_attempt_at = now()
        work = await self.claim(identity)
        if work:
            await self.process(work)
        async with self.database.sessions() as session:
            row = await session.get(VlessSync, identity)
            if not work or row.outcome not in {"confirmed", "missing"}:
                raise ValueError(
                    "Состояние VLESS пока не подтверждено. Повторите обновление позже."
                )
            if row.outcome == "missing":
                raise ValueError(
                    "Ключ не найден в панели. Нужна проверка; оплаченный срок сохранён."
                )
