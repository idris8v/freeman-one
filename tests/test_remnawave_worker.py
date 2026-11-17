"""I12 real PostgreSQL pool/transaction checks; provider HTTP is mocked."""

import asyncio
import json
import os
import time
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from test_billing_concurrency import BillingConcurrency
from test_remnawave_stats import PanelStatistics, users
from test_remnawave_stats import stats as statistics
from test_vless_sync import VlessSynchronization

from app.database.models import (
    AppSetting,
    RemnawaveServer,
    Subscription,
    User,
    VlessCredential,
    VlessSync,
    VlessSyncGate,
)
from app.database.repositories import SQLRepository
from app.services import remnawave_worker as panels
from app.services.remnawave import RemnawaveApiError, RemnawaveWorker
from app.services.vless_sync import VlessReconciler


async def tiny_pool(case):
    url = case.db.engine.url
    await case.db.engine.dispose()
    case.db.engine = create_async_engine(
        url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
        hide_parameters=True,
        connect_args={
            "server_settings": {
                "search_path": case.pg_schema,
                "application_name": "freemanone-i12-fixture",
            }
        },
    )
    case.db.sessions.configure(bind=case.db.engine)


async def no_idle_transaction(case):
    async with case.pg_admin.connect() as connection:
        states = (
            await connection.execute(
                text(
                    "SELECT application_name,state,xact_start FROM pg_stat_activity "
                    "WHERE datname=current_database() AND application_name IN "
                    "('freemanone-i12-fixture','freemanone-vless-journal')"
                )
            )
        ).all()
    case.assertTrue(
        all(
            name == "freemanone-i12-fixture" and state == "idle" and started is None
            for name, state, started in states
        ),
        states,
    )


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class WorkerResources(unittest.IsolatedAsyncioTestCase):
    state = PanelStatistics.state

    async def asyncSetUp(self):
        await PanelStatistics.asyncSetUp(self)
        await tiny_pool(self)
        self.worker = RemnawaveWorker(self.db, self.settings)
        # These I12 fixtures measure one worker domain at its HTTP barrier.
        # HWID has separate resource and combined-lifecycle tests; its short DB
        # claims must not be mistaken for a connection held by panel/I04 HTTP.
        hwid = patch(
            "app.services.vless_hwid_worker.HwidReconciler.once", new_callable=AsyncMock
        )
        hwid.start()
        self.addCleanup(hwid.stop)
        self.api.stats.side_effect = lambda: statistics()
        item = patch(
            "app.services.vless_sync.VlessReconciler.once", new_callable=AsyncMock
        )
        self.keys = item.start()
        self.addCleanup(item.stop)

    async def metadata(self, identity=None):
        async with self.db.sessions() as session:
            row = await session.get(AppSetting, panels.key(identity or self.pid))
            return json.loads(row.value) if row else {}

    async def due(self, *, expire=False):
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            state = await self.metadata()
            state["next_attempt_at"] = 0
            if expire:
                state["lease_until"] = 0
            await repo.set_setting(panels.key(self.pid), json.dumps(state))

    async def add_panel(self, number):
        async with self.db.sessions.begin() as session:
            panel = RemnawaveServer(
                name=f"fixture {number}",
                country="TEST",
                api_url=f"https://panel-{number}.invalid",
                api_token="fixture",
            )
            session.add(panel)
            await session.flush()
            return panel.id

    def api_for(self, apis):
        item = patch(
            "app.services.remnawave.RemnawaveService.api",
            side_effect=lambda p: apis[p.id],
        )
        item.start()
        self.addCleanup(item.stop)

    def fast_api(self):
        api = AsyncMock()
        api.health.return_value = {}
        api.stats.side_effect = lambda: statistics()
        api.users.return_value = users()
        return api

    async def test_original_slow_panel_does_not_hold_request_pool(self):
        arrived, release = asyncio.Event(), asyncio.Event()

        async def health():
            arrived.set()
            await release.wait()
            return {}

        self.api.health.side_effect = health
        with patch("app.services.vless_sync.VlessReconciler.once"):
            task = asyncio.create_task(self.worker.once())
            try:
                await asyncio.wait_for(arrived.wait(), 5)
                self.assertEqual(self.db.engine.pool.checkedout(), 0)
                await no_idle_transaction(self)
                async with self.db.sessions.begin() as session:
                    self.assertEqual(await session.scalar(text("SELECT 1")), 1)
            finally:
                release.set()
                await asyncio.wait_for(task, 10)

    async def test_slow_panel_does_not_block_paid_customer_renewal(self):
        arrived, release = asyncio.Event(), asyncio.Event()

        async def health():
            arrived.set()
            await release.wait()
            return {}

        self.api.health.side_effect = health
        task = asyncio.create_task(self.worker.once())
        try:
            await asyncio.wait_for(arrived.wait(), 5)
            response = await asyncio.wait_for(
                BillingConcurrency.checkout(self, self.sid, 1, "i12-balance-renewal"), 5
            )
            self.assertEqual(response.status_code, 200, response.text)
            async with self.db.sessions() as session:
                self.assertGreater(
                    (await session.get(Subscription, self.sid)).expires_at, self.expiry
                )
                self.assertEqual(await SQLRepository(session).balance(self.uid), 90_000)
        finally:
            release.set()
            await asyncio.wait_for(task, 10)

    async def test_other_panel_and_key_lane_finish_while_first_panel_waits(self):
        second = await self.add_panel(2)
        ready, release = asyncio.Event(), asyncio.Event()
        healthy = self.fast_api()

        async def health():
            ready.set()
            await release.wait()
            return {}

        self.api.health.side_effect = health
        self.api_for({self.pid: self.api, second: healthy})
        task = asyncio.create_task(self.worker.once())
        try:
            await asyncio.wait_for(ready.wait(), 5)
            for _ in range(100):
                async with self.db.sessions() as session:
                    good = (
                        await session.get(RemnawaveServer, second)
                    ).health == "online"
                if good:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(good)
            self.keys.assert_awaited_once()
            self.assertFalse(task.done())
        finally:
            release.set()
            await asyncio.wait_for(task, 10)

    async def test_concurrent_workers_do_not_duplicate_active_panel_check(self):
        ready, release = asyncio.Event(), asyncio.Event()

        async def health():
            ready.set()
            await release.wait()
            return {}

        self.api.health.side_effect = health
        task = asyncio.create_task(self.worker.once())
        try:
            await asyncio.wait_for(ready.wait(), 5)
            await asyncio.wait_for(RemnawaveWorker(self.db, self.settings).once(), 2)
            self.assertEqual(self.api.health.await_count, 1)
            self.assertTrue((await self.metadata())["lease_token"])
        finally:
            release.set()
            await asyncio.wait_for(task, 10)
        await self.worker.once()
        self.assertEqual(self.api.health.await_count, 1)

    async def test_success_schedule_survives_restart(self):
        await self.worker.once()
        state = await self.metadata()
        self.assertEqual(state["outcome"], "confirmed")
        self.assertFalse(state["lease_token"])
        await self.db.engine.dispose()
        await RemnawaveWorker(self.db, self.settings).once()
        self.assertEqual(self.api.health.await_count, 1)

    async def test_repeated_outages_back_off_and_recover(self):
        self.api.health.side_effect = RemnawaveApiError("offline", 503)
        for number in range(1, 8):
            await self.worker.once()
            state = await self.metadata()
            expected = min(number, 6)
            self.assertEqual(state["failures"], expected)
            self.assertAlmostEqual(
                state["next_attempt_at"] - state["last_attempt_at"],
                min(300, 15 * 2 ** (expected - 1)),
                delta=1,
            )
            await self.worker.once()
            self.assertEqual(self.api.health.await_count, number)
            await self.due()
        self.api.health.side_effect = None
        await self.worker.once()
        self.assertEqual((await self.metadata())["failures"], 0)
        self.assertEqual((await self.state()).health, "online")

    async def test_one_budget_bounds_all_three_http_calls(self):
        cancelled = asyncio.Event()

        async def health():
            await asyncio.sleep(0.07)
            return {}

        async def stats():
            await asyncio.sleep(0.07)
            return statistics()

        async def listing(*_):
            try:
                await asyncio.sleep(1)
                return users()
            finally:
                cancelled.set()

        (
            self.api.health.side_effect,
            self.api.stats.side_effect,
            self.api.users.side_effect,
        ) = health, stats, listing
        started = time.monotonic()
        with patch.object(panels, "REQUEST_SECONDS", 0.2):
            await self.worker.once()
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual((await self.state()).traffic_used, 99)
        self.assertEqual((await self.metadata())["outcome"], "unavailable")
        self.assertEqual(self.db.engine.pool.checkedout(), 0)

    async def test_hung_health_times_out_and_releases_lease(self):
        async def health():
            await asyncio.Event().wait()

        self.api.health.side_effect = health
        with patch.object(panels, "REQUEST_SECONDS", 0.05):
            await asyncio.wait_for(self.worker.once(), 3)
        self.assertFalse((await self.metadata())["lease_token"])
        self.assertEqual((await self.state()).traffic_used, 99)
        self.assertEqual(self.db.engine.pool.checkedout(), 0)

    async def test_unexpected_panel_failure_does_not_stop_other_panel(self):
        second = await self.add_panel(2)
        self.api.health.side_effect = RuntimeError("fixture failure")
        self.api_for({self.pid: self.api, second: self.fast_api()})
        with self.assertLogs("app.services.remnawave_worker", level="ERROR"):
            await self.worker.once()
        self.assertEqual((await self.metadata())["outcome"], "unavailable")
        async with self.db.sessions() as session:
            self.assertEqual(
                (await session.get(RemnawaveServer, second)).health, "online"
            )

    async def test_concurrency_is_bounded_for_many_panels(self):
        identities = [self.pid] + [await self.add_panel(n) for n in range(2, 7)]
        active = maximum = started = 0
        ready, release = asyncio.Event(), asyncio.Event()

        async def health():
            nonlocal active, maximum, started
            active += 1
            started += 1
            maximum = max(maximum, active)
            if active == 2:
                ready.set()
            try:
                await release.wait()
            finally:
                active -= 1
            return {}

        apis = {identity: self.fast_api() for identity in identities}
        for api in apis.values():
            api.health.side_effect = health
        self.api_for(apis)
        task = asyncio.create_task(self.worker.once())
        try:
            await asyncio.wait_for(ready.wait(), 5)
            await asyncio.sleep(0.05)
            self.assertEqual(started, 2)
            self.assertEqual(self.db.engine.pool.checkedout(), 0)
        finally:
            release.set()
            await asyncio.wait_for(task, 10)
        self.assertEqual(maximum, 2)
        self.assertEqual(started, len(identities))

    async def test_cancellation_keeps_expiring_lease_for_safe_restart(self):
        ready, cancelled = asyncio.Event(), asyncio.Event()

        async def health():
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.api.health.side_effect = health
        task = asyncio.create_task(self.worker.once())
        await asyncio.wait_for(ready.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.db.engine.pool.checkedout(), 0)
        self.assertTrue((await self.metadata())["lease_token"])
        await RemnawaveWorker(self.db, self.settings).once()
        self.assertEqual(self.api.health.await_count, 1)
        await self.due(expire=True)
        self.api.health.side_effect = None
        await RemnawaveWorker(self.db, self.settings).once()
        self.assertEqual(self.api.health.await_count, 2)

    async def test_old_lease_owner_cannot_finish_successor(self):
        old = await panels.claim(self.db, self.pid)
        await self.due(expire=True)
        new = await panels.claim(self.db, self.pid)
        self.assertNotEqual(old.token, new.token)
        await panels.finish(self.db, old, True)
        self.assertEqual((await self.metadata())["lease_token"], new.token)
        await panels.finish(self.db, new, False, "fixture")
        self.assertFalse((await self.metadata())["lease_token"])

    async def test_disabled_panel_is_not_checked(self):
        async with self.db.sessions.begin() as session:
            (await session.get(RemnawaveServer, self.pid)).status = "disabled"
        await self.worker.once()
        self.api.health.assert_not_awaited()
        self.assertEqual(await self.metadata(), {})

    async def test_failed_bookkeeping_transaction_keeps_lease_until_recovery(self):
        second = await self.add_panel(2)
        self.api_for({self.pid: self.api, second: self.fast_api()})
        original = panels.finish

        async def failed(database, work, success, error=""):
            if work.panel.id == self.pid:
                async with database.sessions.begin() as session:
                    await session.execute(text("SELECT 1/0"))
            return await original(database, work, success, error)

        with patch.object(panels, "finish", new=failed):
            with self.assertLogs("app.services.remnawave_worker", level="ERROR"):
                await self.worker.once()
        self.assertTrue((await self.metadata())["lease_token"])
        self.assertEqual((await self.metadata(second))["outcome"], "confirmed")
        self.assertEqual(self.db.engine.pool.checkedout(), 0)
        await self.due(expire=True)
        await RemnawaveWorker(self.db, self.settings).once()
        self.assertFalse((await self.metadata())["lease_token"])


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class KeyWorkerResources(unittest.IsolatedAsyncioTestCase):
    state = VlessSynchronization.state
    due = VlessSynchronization.due
    expire_leases = VlessSynchronization.expire_leases

    async def asyncSetUp(self):
        await VlessSynchronization.asyncSetUp(self)
        await tiny_pool(self)
        self.worker = RemnawaveWorker(self.db, self.settings)
        # These I12 fixtures measure one worker domain at its HTTP barrier.
        # HWID has separate resource and combined-lifecycle tests; its short DB
        # claims must not be mistaken for a connection held by panel/I04 HTTP.
        hwid = patch(
            "app.services.vless_hwid_worker.HwidReconciler.once", new_callable=AsyncMock
        )
        hwid.start()
        self.addCleanup(hwid.stop)

    async def test_slow_key_get_releases_pool_and_all_row_locks(self):
        self.api.read_entered, self.api.read_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.worker.once(refresh_panels=False))
        try:
            await asyncio.wait_for(self.api.read_entered.wait(), 5)
            # The second consumer may still be making its short claim. Prove
            # bounded connection and row-lock availability while HTTP is blocked.
            async with self.db.sessions.begin() as session:
                for model in (
                    User,
                    Subscription,
                    RemnawaveServer,
                    VlessCredential,
                    VlessSync,
                    VlessSyncGate,
                ):
                    self.assertIsNotNone(
                        await session.scalar(select(model).with_for_update(nowait=True))
                    )
            self.assertEqual(self.db.engine.pool.checkedout(), 0)
            await no_idle_transaction(self)
        finally:
            self.api.read_release.set()
            await asyncio.wait_for(task, 10)

    async def test_key_deadline_preserves_usage_and_finishes_existing_leases(self):
        self.api.read_entered, self.api.read_release = asyncio.Event(), asyncio.Event()
        with patch("app.services.vless_sync.REQUEST_SECONDS", 0.1):
            await asyncio.wait_for(self.worker.once(refresh_panels=False), 5)
        credential, _, _, state = await self.state()
        self.assertEqual(credential.traffic_used, 73)
        self.assertEqual(state.outcome, "unavailable")
        self.assertFalse(state.lease_token)
        async with self.db.sessions() as session:
            self.assertFalse((await session.get(VlessSyncGate, self.pid)).lease_token)
        self.assertEqual(self.db.engine.pool.checkedout(), 0)

    async def test_concurrent_key_workers_preserve_i04_single_dispatch(self):
        self.api.read_entered, self.api.read_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.worker.once(refresh_panels=False))
        try:
            await asyncio.wait_for(self.api.read_entered.wait(), 5)
            await asyncio.wait_for(
                RemnawaveWorker(self.db, self.settings).once(refresh_panels=False), 3
            )
            self.assertEqual(len(self.api.lookups), 1)
        finally:
            self.api.read_release.set()
            await asyncio.wait_for(task, 10)

    async def test_key_restart_after_timeout_recovers_without_duplicate_operation(self):
        self.api.read_entered, self.api.read_release = asyncio.Event(), asyncio.Event()
        with patch("app.services.vless_sync.REQUEST_SECONDS", 0.1):
            await self.worker.once(refresh_panels=False)
        await self.expire_leases()
        self.api.read_entered = self.api.read_release = None
        await self.db.engine.dispose()
        await RemnawaveWorker(self.db, self.settings).once(refresh_panels=False)
        credential, _, _, state = await self.state()
        self.assertEqual(state.outcome, "confirmed")
        self.assertEqual(credential.traffic_used, 1234)
        self.assertEqual(len(self.api.lookups), 2)

    async def test_failed_key_claim_does_not_orphan_other_consumer(self):
        original = VlessReconciler.claim
        calls = 0

        async def claim(reconciler, *args):
            nonlocal calls
            calls += 1
            if calls == 1:
                async with self.db.sessions.begin() as session:
                    await session.execute(text("SELECT 1 / 0"))
            return await original(reconciler, *args)

        with (
            patch.object(VlessReconciler, "claim", claim),
            self.assertLogs("app.services.vless_sync", level="ERROR"),
        ):
            await asyncio.wait_for(self.worker.once(refresh_panels=False), 10)
        _, _, _, state = await self.state()
        self.assertEqual(state.outcome, "confirmed")
        self.assertFalse(state.lease_token)
        self.assertEqual(len(self.api.lookups), 1)
        self.assertEqual(self.db.engine.pool.checkedout(), 0)
        await no_idle_transaction(self)

    async def test_failed_key_seed_does_not_cancel_healthy_panel(self):
        with (
            patch.object(VlessReconciler, "seed", side_effect=RuntimeError("fixture")),
            self.assertLogs("app.services.vless_sync", level="ERROR"),
        ):
            await asyncio.wait_for(self.worker.once(), 10)
        async with self.db.sessions() as session:
            panel = await session.get(RemnawaveServer, self.pid)
            self.assertEqual(panel.health, "online")
            row = await session.get(AppSetting, panels.key(self.pid))
            self.assertEqual(json.loads(row.value)["outcome"], "confirmed")
        self.assertEqual(self.db.engine.pool.checkedout(), 0)

    async def test_cancelling_key_worker_awaits_children_and_preserves_lease(self):
        self.api.read_entered, self.api.read_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.worker.once(refresh_panels=False))
        await asyncio.wait_for(self.api.read_entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        _, _, _, state = await self.state()
        self.assertTrue(state.lease_token)
        self.assertEqual(self.db.engine.pool.checkedout(), 0)
        await no_idle_transaction(self)
        await self.expire_leases()
        self.api.read_entered = self.api.read_release = None
        await self.worker.once(refresh_panels=False)
        _, _, _, state = await self.state()
        self.assertEqual(state.outcome, "confirmed")
        self.assertEqual(len(self.api.lookups), 2)

    async def test_brief_panel_commit_retries_only_local_finish_without_locks(self):
        self.api.read_entered, self.api.read_release = asyncio.Event(), asyncio.Event()
        attempted = asyncio.Event()
        original = VlessReconciler._finish

        async def finish(reconciler, *args, **kwargs):
            result = await original(reconciler, *args, **kwargs)
            if result is None:
                attempted.set()
            return result

        with patch.object(VlessReconciler, "_finish", finish):
            task = asyncio.create_task(self.worker.once(refresh_panels=False))
            try:
                await asyncio.wait_for(self.api.read_entered.wait(), 5)
                async with self.pg_admin.begin() as blocker:
                    await blocker.exec_driver_sql(
                        "SET LOCAL search_path TO " + self.pg_schema
                    )
                    await blocker.execute(
                        select(RemnawaveServer)
                        .where(RemnawaveServer.id == self.pid)
                        .with_for_update()
                    )
                    self.api.read_release.set()
                    await asyncio.wait_for(attempted.wait(), 5)
                    async with self.db.sessions.begin() as session:
                        self.assertIsNotNone(
                            await session.scalar(
                                select(Subscription).with_for_update(nowait=True)
                            )
                        )
                    self.assertEqual(self.db.engine.pool.checkedout(), 0)
                await asyncio.wait_for(task, 10)
            finally:
                self.api.read_release.set()
                await asyncio.wait_for(task, 10)
        credential, _, _, state = await self.state()
        self.assertEqual(credential.traffic_used, 1234)
        self.assertEqual(state.outcome, "confirmed")
        self.assertEqual(len(self.api.lookups), 1)

    async def test_persistent_panel_lock_defers_without_blocking_customers(self):
        await self.sync.seed()
        work = await self.sync.claim()
        async with self.pg_admin.begin() as blocker:
            await blocker.exec_driver_sql("SET LOCAL search_path TO " + self.pg_schema)
            await blocker.execute(
                select(RemnawaveServer)
                .where(RemnawaveServer.id == self.pid)
                .with_for_update()
            )
            await asyncio.wait_for(self.sync.process(work), 5)
            credential, _, _, state = await self.state()
            self.assertEqual(state.outcome, "local_busy")
            self.assertEqual(credential.traffic_used, 73)
            self.assertEqual(len(self.api.lookups), 1)
            self.assertEqual(self.db.engine.pool.checkedout(), 0)
            async with self.db.sessions.begin() as session:
                self.assertIsNotNone(
                    await session.scalar(
                        select(Subscription).with_for_update(nowait=True)
                    )
                )
