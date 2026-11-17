"""Short database phases, bounded panel checks and durable background leases."""

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.database.models import AppSetting, RemnawaveServer, now
from app.database.repositories import SQLRepository
from app.services.remnawave import RemnawaveService
from app.services.remnawave_stats import source
from app.services.vless_sync import VlessReconciler
from app.services.vless_hwid_worker import HwidReconciler

CONCURRENCY = 2
REQUEST_SECONDS = 20
LEASE_SECONDS = 60
SWEEP_SECONDS = 15
INTERVAL_SECONDS = 300
logger = logging.getLogger(__name__)


def key(identity):
    return f"remnawave_worker:{identity}"


@dataclass
class Work:
    panel: RemnawaveServer
    token: str
    search_path: str


async def claim(database, identity):
    async with database.sessions.begin() as session:
        if session.bind.dialect.name != "postgresql":
            return None  # Real VLESS background recovery requires PostgreSQL.
        panel = await session.get(RemnawaveServer, identity)
        if panel is None or panel.status != "active":
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
        if state.get("lease_until", 0) > stamp:
            return None
        fingerprint = source(panel)
        if (
            state.get("source") == fingerprint
            and state.get("next_attempt_at", 0) > stamp
        ):
            return None
        if state.get("source") != fingerprint:
            state = {}
        token = secrets.token_hex(16)
        state.update(
            source=fingerprint,
            lease_token=token,
            lease_until=stamp + LEASE_SECONDS,
            last_attempt_at=stamp,
        )
        row.value = json.dumps(state)
        # Capture schema routing while the short claim transaction is open.
        # The idle service session below must never execute SHOW during HTTP.
        path = await session.scalar(text("SHOW search_path"))
        return Work(panel, token, path)


async def finish(database, work, success, error=""):
    async with database.sessions.begin() as session:
        row = await session.scalar(
            select(AppSetting)
            .where(AppSetting.key == key(work.panel.id))
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return
        state = json.loads(row.value)
        if state.get("lease_token") != work.token:
            return  # A crashed/expired worker cannot finish its successor's work.
        stamp = now().timestamp()
        failures = 0 if success else min(state.get("failures", 0) + 1, 6)
        delay = (
            INTERVAL_SECONDS
            if success
            else min(INTERVAL_SECONDS, 15 * 2 ** (failures - 1))
        )
        state.update(
            lease_token="",
            lease_until=0,
            failures=failures,
            next_attempt_at=stamp + delay,
            outcome="confirmed" if success else "unavailable",
            last_error=error[:255],
        )
        if success:
            state["last_success_at"] = stamp
        row.value = json.dumps(state)


async def once(worker, refresh_panels=True):
    database = worker.database
    identities = []
    if refresh_panels:
        async with database.sessions() as session:
            identities = list(
                await session.scalars(
                    select(RemnawaveServer.id)
                    .where(RemnawaveServer.status == "active")
                    .order_by(RemnawaveServer.id)
                )
            )
    pending = iter(identities)

    async def consume():
        for identity in pending:
            work = None
            try:
                work = await claim(database, identity)
                if work is None:
                    continue
                # Detached panel + an unused session with cached schema routing.
                # No pool connection/transaction is checked out during HTTP.
                async with database.sessions(
                    info={"vless_journal_search_path": work.search_path}
                ) as session:
                    success = await RemnawaveService(
                        SQLRepository(session), worker.settings
                    ).refresh(work.panel, request_timeout=REQUEST_SECONDS)
                await finish(
                    database,
                    work,
                    success,
                    "" if success else "Panel statistics unavailable or superseded",
                )
            except asyncio.CancelledError:
                # Like I04, retain the expiring lease on shutdown/hard interruption.
                # Restart recovery cannot dispatch a duplicate live operation.
                raise
            except Exception as error:
                logger.exception("RemnaWave panel refresh failed: %s", identity)
                if work:
                    try:
                        await finish(database, work, False, type(error).__name__)
                    except Exception:
                        logger.exception(
                            "RemnaWave panel lease finish failed: %s", identity
                        )

    # Keep existing panel checks and per-key reconciliation in the same worker,
    # but do not make keys/other panels wait for a slow panel's external requests.
    async with asyncio.TaskGroup() as tasks:
        for _ in range(min(CONCURRENCY, len(identities))):
            tasks.create_task(consume())
        tasks.create_task(VlessReconciler(database, worker.settings).once())
        tasks.create_task(HwidReconciler(database, worker.settings).once())
