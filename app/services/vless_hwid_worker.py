"""Bounded, read-only HWID refresh with durable leases and short DB phases."""

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import Float, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB, insert

from app.database.models import (
    AccessKey,
    AppSetting,
    RemnawaveServer,
    VlessCredential,
    VlessRenewal,
    now,
)
from app.database.repositories import SQLRepository
from app.services import vless_hwid
from app.services.remnawave import RemnawaveService
from app.services.remnawave_stats import source
from app.services.vless_renewal import PENDING

BATCH = 20
CONCURRENCY = 2
REQUEST_SECONDS = 10
SWEEP_SECONDS = 12
LEASE_SECONDS = 45
INTERVAL_SECONDS = 300
RATE_SECONDS = 0.25
logger = logging.getLogger(__name__)


def key(identity):
    return f"vless_hwid_worker:{identity}"


@dataclass(frozen=True)
class Work:
    panel: RemnawaveServer
    observation: vless_hwid.Observation
    token: str
    panel_source: str


class HwidReconciler:
    def __init__(self, database, settings, *, rate_seconds=RATE_SECONDS):
        self.database, self.settings, self.rate_seconds = (
            database,
            settings,
            rate_seconds,
        )

    async def candidates(self):
        async with self.database.sessions() as session:
            if session.bind.dialect.name != "postgresql":
                return []
            metadata = cast(AppSetting.value, JSONB)
            attempt = func.coalesce(metadata["last_attempt_at"].astext.cast(Float), 0)
            queue = (
                select(
                    VlessCredential.id.label("id"),
                    attempt.label("attempt"),
                    func.row_number()
                    .over(
                        partition_by=VlessCredential.server_id,
                        order_by=(attempt, VlessCredential.id),
                    )
                    .label("position"),
                )
                .join(AccessKey, AccessKey.id == VlessCredential.access_key_id)
                .join(RemnawaveServer, RemnawaveServer.id == VlessCredential.server_id)
                .outerjoin(
                    AppSetting,
                    AppSetting.key
                    == func.concat("vless_hwid_worker:", VlessCredential.id),
                )
                .where(
                    VlessCredential.status != "deleted",
                    AccessKey.status != "deleted",
                    RemnawaveServer.status == "active",
                    VlessCredential.remote_user_uuid != "",
                    ~select(VlessRenewal.id)
                    .where(
                        VlessRenewal.subscription_id == VlessCredential.subscription_id,
                        VlessRenewal.state.in_(PENDING),
                    )
                    .exists(),
                    func.coalesce(metadata["next_attempt_at"].astext.cast(Float), 0)
                    <= now().timestamp(),
                    func.coalesce(metadata["lease_until"].astext.cast(Float), 0)
                    <= now().timestamp(),
                )
                .subquery()
            )
            # Round-robin panels before taking the bounded batch. An unavailable
            # panel with many users must not monopolize the candidate list.
            return list(
                await session.scalars(
                    select(queue.c.id)
                    .order_by(queue.c.position, queue.c.attempt, queue.c.id)
                    .limit(BATCH)
                )
            )

    async def claim(self, identity):
        async with self.database.sessions.begin() as session:
            credential = await session.get(VlessCredential, identity)
            if not credential or credential.status == "deleted":
                return None
            panel = await session.get(RemnawaveServer, credential.server_id)
            access = await session.get(AccessKey, credential.access_key_id)
            if (
                not panel
                or panel.status != "active"
                or not access
                or access.status == "deleted"
            ):
                return None
            # Leave active I02 recovery alone. HWID never changes subscription terms.
            if await session.scalar(
                select(VlessRenewal.id)
                .where(
                    VlessRenewal.subscription_id == credential.subscription_id,
                    VlessRenewal.state.in_(PENDING),
                )
                .limit(1)
            ):
                return None
            await session.execute(
                insert(AppSetting)
                .values(key=key(identity), value="{}")
                .on_conflict_do_nothing()
            )
            row = await session.scalar(
                select(AppSetting)
                .where(AppSetting.key == key(identity))
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            state, stamp = json.loads(row.value), now().timestamp()
            if (
                state.get("lease_until", 0) > stamp
                or state.get("next_attempt_at", 0) > stamp
            ):
                return None
            # One dispatch slot per panel across replicas, independent of I04's
            # leases. No lock survives the short claim transaction.
            rate_key = f"vless_hwid_rate:{panel.id}"
            await session.execute(
                insert(AppSetting)
                .values(key=rate_key, value="0")
                .on_conflict_do_nothing()
            )
            rate = await session.scalar(
                select(AppSetting)
                .where(AppSetting.key == rate_key)
                .with_for_update(skip_locked=True)
            )
            if rate is None or float(rate.value) > stamp:
                return None
            rate.value = str(stamp + self.rate_seconds)
            token = secrets.token_hex(16)
            state.update(
                lease_token=token,
                lease_until=stamp + LEASE_SECONDS,
                last_attempt_at=stamp,
            )
            row.value = json.dumps(state)
            observation = await vless_hwid.capture(SQLRepository(session), credential)
            return Work(panel, observation, token, source(panel))

    async def finish(self, work, devices=None, error=""):
        async with self.database.sessions.begin() as session:
            row = await session.scalar(
                select(AppSetting)
                .where(AppSetting.key == key(work.observation.credential_id))
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return
            state = json.loads(row.value)
            if state.get("lease_token") != work.token:
                return  # An expired worker never applies its successor's response.
            panel = await session.get(RemnawaveServer, work.panel.id)
            applied = None
            if (
                devices is not None
                and panel
                and source(panel) == work.panel_source
                and panel.status == "active"
            ):
                applied = await vless_hwid.apply_snapshot(
                    SQLRepository(session), work.observation, devices, skip_locked=True
                )
            success = applied is not None  # Valid empty dictionary means known zero.
            stamp = now().timestamp()
            failures = 0 if success else min(state.get("failures", 0) + 1, 6)
            state.update(
                lease_token="",
                lease_until=0,
                failures=failures,
                next_attempt_at=stamp
                + (INTERVAL_SECONDS if success else min(300, 15 * 2 ** (failures - 1))),
                outcome="confirmed" if success else "unavailable",
                last_error=""
                if success
                else error or "Superseded or unavailable snapshot",
            )
            if success:
                state["last_success_at"] = stamp
            row.value = json.dumps(state)

    async def fetch(self, work):
        # The session is deliberately unused: the API factory reads only detached
        # panel credentials, so zero connections are checked out during HTTP.
        async with self.database.sessions() as session:
            api = RemnawaveService(SQLRepository(session), self.settings).api(
                work.panel
            )
            payload = await asyncio.wait_for(
                api.devices(work.observation.source[1]), REQUEST_SECONDS
            )
        return vless_hwid.parse_devices(payload, work.observation.source[1])

    async def once(self):
        try:
            pending = iter(await self.candidates())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "HWID candidate scan failed; other worker domains continue"
            )
            return

        async def consume():
            for identity in pending:
                work = None
                try:
                    work = await self.claim(identity)
                    if work is not None:
                        try:
                            devices = await self.fetch(work)
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            await self.finish(work, error=type(error).__name__)
                        else:
                            await self.finish(work, devices)
                except asyncio.CancelledError:
                    # Keep an expiring lease through shutdown/restart. Remote
                    # reads are harmless; any late result remains generation-fenced.
                    raise
                except Exception:
                    logger.exception(
                        "HWID cache refresh failed for credential %s", identity
                    )

        try:
            async with asyncio.timeout(SWEEP_SECONDS), asyncio.TaskGroup() as tasks:
                for _ in range(CONCURRENCY):
                    tasks.create_task(consume())
        except TimeoutError:
            logger.info(
                "HWID sweep budget reached; unfinished leases recover automatically"
            )
