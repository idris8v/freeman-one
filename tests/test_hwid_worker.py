"""HWID background cache: PostgreSQL leases/resources; no live panel access."""

import asyncio
import json
import os
import unittest
from unittest.mock import patch

from sqlalchemy import delete, select, text
import test_vless_devices as devices
import test_remnawave_worker as resources
from app.database.models import AppSetting, Device, Subscription, VlessCredential
from app.database.repositories import SQLRepository
from app.services import vless_hwid
from app.services.vless_hwid_worker import HwidReconciler, key
from app.services.remnawave import RemnawaveApiError


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class HwidWorker(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await devices.VlessDevices.asyncSetUp(self)
        self.settings.admin_ids = "123"
        self.worker = HwidReconciler(self.db, self.settings, rate_seconds=0)
        self.api.devices.return_value = devices.snapshot(devices.hwid("a"))

    async def count(self):
        async with self.db.sessions() as session:
            return await vless_hwid.cache_state(session, self.sid)

    async def due(self, expire=False):
        async with self.db.sessions.begin() as session:
            row = await session.get(AppSetting, key(self.cid))
            state = json.loads(row.value)
            state["next_attempt_at"] = 0
            if expire:
                state["lease_until"] = 0
            row.value = json.dumps(state)

    async def test_background_updates_count_filters_without_per_row_http(self):
        async with self.db.sessions.begin() as session:
            (await session.get(Subscription, self.sid)).device_limit = 3
        await self.worker.once()
        self.assertEqual((await self.count())[0], 1)
        calls = self.api.devices.await_count
        for filter_, expected in [
            ("none", 0),
            ("available", 1),
            ("connected", 1),
            ("unknown", 0),
        ]:
            r = await self.web.get("/api/web/admin/keys", params={"devices": filter_})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["total"], expected)
            if expected:
                self.assertEqual(
                    (
                        r.json()["items"][0]["device_count"],
                        r.json()["items"][0]["device_limit"],
                    ),
                    (1, 3),
                )
        self.assertEqual(self.api.devices.await_count, calls)
        await HwidReconciler(self.db, self.settings).once()
        self.assertEqual(self.api.devices.await_count, calls)

    async def test_authoritative_zero_and_unlimited_free_slots(self):
        async with self.db.sessions.begin() as session:
            await session.execute(delete(Device))
        self.api.devices.return_value = devices.snapshot()
        await self.worker.once()
        self.assertEqual((await self.count())[0], 0)
        r = await self.web.get("/api/web/admin/keys", params={"devices": "none"})
        self.assertEqual(r.json()["total"], 1)
        async with self.db.sessions.begin() as session:
            (await session.get(Subscription, self.sid)).device_limit = 0
        r = await self.web.get("/api/web/admin/keys", params={"devices": "available"})
        self.assertEqual(r.json()["total"], 1)
        self.api.devices.return_value = devices.snapshot(
            devices.hwid("a"), devices.hwid("b")
        )
        await self.due()
        await self.worker.once()
        r = await self.web.get("/api/web/admin/keys", params={"devices": "available"})
        self.assertEqual(r.json()["total"], 1)
        self.assertFalse(r.json()["items"][0]["device_over_limit"])

    async def test_malformed_responses_and_outage_preserve_last_good_state(self):
        await self.worker.once()
        for payload in devices.MALFORMED.values():
            self.api.devices.return_value = payload
            await self.due()
            await self.worker.once()
            self.assertEqual((await self.count())[0], 1)
        self.api.devices.side_effect = RemnawaveApiError("fixture outage", 503)
        await self.due()
        await self.worker.once()
        self.assertEqual((await self.count())[0], 1)
        self.api.devices.side_effect = None
        self.api.devices.return_value = devices.snapshot(
            devices.hwid("a"), devices.hwid("b")
        )
        await self.due()
        await self.worker.once()
        self.assertEqual((await self.count())[0], 2)

    async def test_unknown_failure_never_becomes_zero(self):
        async with self.db.sessions.begin() as session:
            await session.execute(delete(Device))
        self.api.devices.side_effect = RemnawaveApiError("fixture timeout")
        await self.worker.once()
        self.assertEqual((await self.count())[0], None)
        r = await self.web.get("/api/web/admin/keys", params={"devices": "none"})
        self.assertEqual(r.json()["total"], 0)
        r = await self.web.get("/api/web/admin/keys", params={"devices": "unknown"})
        self.assertEqual(r.json()["total"], 1)

    async def test_slow_http_releases_database_and_concurrent_worker_skips_lease(self):
        await resources.tiny_pool(self)
        arrived, release = asyncio.Event(), asyncio.Event()

        async def slow(_):
            arrived.set()
            await release.wait()
            return devices.snapshot()

        self.api.devices.side_effect = slow
        task = asyncio.create_task(self.worker.once())
        try:
            await asyncio.wait_for(arrived.wait(), 5)
            self.assertEqual(self.db.engine.pool.checkedout(), 0)
            await resources.no_idle_transaction(self)
            async with self.db.sessions.begin() as session:
                self.assertEqual(await session.scalar(text("SELECT 1")), 1)
            await asyncio.wait_for(HwidReconciler(self.db, self.settings).once(), 3)
            self.assertEqual(self.api.devices.await_count, 1)
        finally:
            release.set()
            await task

    async def test_explicit_removal_invalidates_in_flight_response(self):
        work = await self.worker.claim(self.cid)
        async with self.db.sessions.begin() as session:
            c = await session.get(VlessCredential, self.cid)
            await vless_hwid.confirmed_removal(SQLRepository(session), c, "a")
        await self.worker.finish(work, [devices.hwid("a"), devices.hwid("b")])
        self.assertEqual((await self.count())[0], 1)
        async with self.db.sessions() as session:
            self.assertEqual(
                (
                    await session.scalar(
                        select(Device).where(Device.provisioning_id == "a")
                    )
                ).status,
                "disconnected",
            )

    async def test_cancel_and_restart_recover_expiring_lease(self):
        arrived = asyncio.Event()

        async def hang(_):
            arrived.set()
            await asyncio.Future()

        self.api.devices.side_effect = hang
        task = asyncio.create_task(self.worker.once())
        await asyncio.wait_for(arrived.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await HwidReconciler(self.db, self.settings).once()
        self.assertEqual(self.api.devices.await_count, 1)
        await self.due(expire=True)
        self.api.devices.side_effect = None
        await HwidReconciler(self.db, self.settings).once()
        self.assertEqual(self.api.devices.await_count, 2)
        self.assertEqual((await self.count())[0], 1)

    async def test_old_lease_response_cannot_overwrite_successor(self):
        old = await self.worker.claim(self.cid)
        await self.due(expire=True)
        new = await self.worker.claim(self.cid)
        await self.worker.finish(new, [devices.hwid("new")])
        await self.worker.finish(old, [])
        self.assertEqual((await self.count())[0], 1)
        async with self.db.sessions() as session:
            active = list(
                await session.scalars(
                    select(Device.provisioning_id).where(Device.status == "active")
                )
            )
            self.assertEqual(active, ["new"])

    async def test_timeout_is_bounded_and_cache_is_preserved(self):
        async def hang(_):
            await asyncio.Future()

        self.api.devices.side_effect = hang
        with patch("app.services.vless_hwid_worker.REQUEST_SECONDS", 0.01):
            await asyncio.wait_for(self.worker.once(), 2)
        self.assertEqual((await self.count())[0], 2)

    async def test_failed_local_apply_keeps_old_cache_and_restart_recovers(self):
        from sqlalchemy.exc import SQLAlchemyError

        real_apply = vless_hwid.apply_snapshot

        async def fail(*args, **kwargs):
            await real_apply(*args, **kwargs)
            raise SQLAlchemyError("fixture failed commit after apply")

        work = await self.worker.claim(self.cid)
        with patch("app.services.vless_hwid.apply_snapshot", side_effect=fail):
            with self.assertRaises(SQLAlchemyError):
                await self.worker.finish(work, [devices.hwid("a")])
        self.assertEqual((await self.count())[0], 2)
        await self.due(expire=True)
        await HwidReconciler(self.db, self.settings).once()
        self.assertEqual((await self.count())[0], 1)

    async def test_panel_rate_limit_is_shared_across_replicas(self):
        worker = HwidReconciler(self.db, self.settings, rate_seconds=60)
        self.assertIsNotNone(await worker.claim(self.cid))
        await self.due(expire=True)
        self.assertIsNone(await HwidReconciler(self.db, self.settings).claim(self.cid))

    async def test_round_robin_batch_and_parallelism_are_bounded(self):
        from app.database.models import AccessKey, RemnawaveServer

        async with self.db.sessions.begin() as session:
            old = await session.get(VlessCredential, self.cid)
            panel = RemnawaveServer(
                name="Other fixture",
                country="TEST",
                api_url="https://other.invalid",
                api_token="fixture",
            )
            session.add(panel)
            await session.flush()
            for index in range(25):
                sub = Subscription(
                    user_id=self.uid, kind="paid", expires_at=self.expiry
                )
                k = AccessKey(
                    user_id=self.uid,
                    name="Fixture",
                    mode="vless",
                    expires_at=self.expiry,
                )
                session.add_all([sub, k])
                await session.flush()
                c = VlessCredential(
                    server_id=panel.id if index == 24 else old.server_id,
                    user_id=self.uid,
                    subscription_id=sub.id,
                    access_key_id=k.id,
                    remote_user_uuid=str(200 + index),
                    subscription_url="https://sub.example.test/mock",
                    expires_at=self.expiry,
                    device_limit=1,
                    status="active",
                )
                session.add(c)
                await session.flush()
                if index == 24:
                    other = c.id
        candidates = await self.worker.candidates()
        self.assertEqual(len(candidates), 20)
        self.assertIn(other, candidates[:2])
        active = peak = 0

        async def fetch(identity):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.002)
                return devices.snapshot(devices.hwid(userId=int(identity)))
            finally:
                active -= 1

        self.api.devices.side_effect = fetch
        await self.worker.once()
        self.assertLessEqual(self.api.devices.await_count, 20)
        self.assertLessEqual(peak, 2)

    async def test_total_sweep_deadline_keeps_recoverable_lease(self):
        deadline = asyncio.timeout(None)

        async def hang(_):
            # Expire the real timeout only after the durable claim and HTTP
            # boundary, rather than timing the database with a 10 ms deadline.
            deadline.reschedule(asyncio.get_running_loop().time())
            await asyncio.Future()

        self.api.devices.side_effect = hang
        with patch(
            "app.services.vless_hwid_worker.asyncio.timeout", return_value=deadline
        ):
            await asyncio.wait_for(self.worker.once(), 5)
        self.assertEqual((await self.count())[0], 2)
        async with self.db.sessions() as session:
            state = json.loads((await session.get(AppSetting, key(self.cid))).value)
            self.assertGreater(state["lease_until"], state["last_attempt_at"])

    async def test_locked_customer_subscription_does_not_block_background_apply(self):
        work = await self.worker.claim(self.cid)
        async with self.db.sessions.begin() as session:
            await session.scalar(
                select(Subscription)
                .where(Subscription.id == self.sid)
                .with_for_update()
            )
            await asyncio.wait_for(self.worker.finish(work, [devices.hwid("a")]), 1)
        self.assertEqual((await self.count())[0], 2)

    async def test_combined_worker_http_does_not_exhaust_single_connection_pool(self):
        from app.services.remnawave import RemnawaveWorker

        await resources.tiny_pool(self)
        arrived = {name: asyncio.Event() for name in ("panel", "key", "hwid")}
        release = asyncio.Event()

        async def wait(name):
            arrived[name].set()
            await release.wait()
            return {}

        # AsyncMock side effects must await coroutines, not return a coroutine.
        async def health():
            return await wait("panel")

        async def user(_):
            return await wait("key")

        async def hwid(_):
            return await wait("hwid")

        self.api.health.side_effect = health
        self.api.user.side_effect = user
        self.api.devices.side_effect = hwid
        task = asyncio.create_task(RemnawaveWorker(self.db, self.settings).once())
        try:
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in arrived.values())), 5
            )
            async with self.db.sessions.begin() as session:
                self.assertEqual(
                    await asyncio.wait_for(session.scalar(text("SELECT 1")), 1), 1
                )
                self.assertIsNotNone(
                    await session.scalar(
                        select(Subscription.id)
                        .where(Subscription.id == self.sid)
                        .with_for_update(nowait=True)
                    )
                )
        finally:
            release.set()
            await task

    async def test_pending_renewal_is_not_polled_until_recovery_finishes(self):
        from datetime import timedelta
        from app.database.models import VlessRenewal

        async with self.db.sessions.begin() as session:
            credential = await session.get(VlessCredential, self.cid)
            session.add(
                VlessRenewal(
                    id="pending-hwid-fixture",
                    user_id=self.uid,
                    subscription_id=self.sid,
                    credential_id=self.cid,
                    key_id=self.kid,
                    server_id=credential.server_id,
                    server_url="https://panel.invalid",
                    remote_identity="123",
                    request="{}",
                    base_expires_at=self.expiry,
                    base_device_limit=1,
                    expires_at=self.expiry + timedelta(days=30),
                    device_limit=1,
                    target_status="ACTIVE",
                    state="outcome_unknown",
                )
            )
        self.assertEqual(await self.worker.candidates(), [])
        await self.worker.once()
        self.api.devices.assert_not_awaited()
        async with self.db.sessions.begin() as session:
            (await session.get(VlessRenewal, "pending-hwid-fixture")).state = "applied"
        await self.worker.once()
        self.api.devices.assert_awaited_once()

    async def test_candidate_scan_failure_does_not_cancel_other_worker_domains(self):
        from app.services.remnawave import RemnawaveWorker

        with patch(
            "app.services.vless_hwid_worker.HwidReconciler.candidates",
            side_effect=RuntimeError("fixture scan failure"),
        ):
            with self.assertLogs("app.services.vless_hwid_worker", level="ERROR"):
                await RemnawaveWorker(self.db, self.settings).once()
        self.assertGreater(self.api.health.await_count, 0)
        self.assertEqual((await self.count())[0], 2)

    async def test_reassigned_credential_invalidates_older_observation(self):
        work = await self.worker.claim(self.cid)
        async with self.db.sessions.begin() as session:
            (await session.get(VlessCredential, self.cid)).user_id = self.other
        await self.worker.finish(work, [])
        self.assertEqual((await self.count())[0], 2)

    async def test_unsupported_provider_is_unknown_not_zero_or_hwid(self):
        from app.database.models import AccessKey, VpnCredential, VpnServer

        async with self.db.sessions.begin() as session:
            await session.delete(await session.get(VlessCredential, self.cid))
            (await session.get(AccessKey, self.kid)).mode = "amnezia"
            server = VpnServer(
                name="Fixture Amnezia",
                country="TEST",
                api_url="https://amnezia.invalid",
                api_key="fixture",
            )
            session.add(server)
            await session.flush()
            session.add(
                VpnCredential(
                    server_id=server.id,
                    user_id=self.uid,
                    subscription_id=self.sid,
                    access_key_id=self.kid,
                    remote_client_id="fixture",
                    config="fixture",
                    expires_at=self.expiry,
                )
            )
        for f, expected in [("unknown", 1), ("none", 0), ("available", 0)]:
            r = await self.web.get("/api/web/admin/keys", params={"devices": f})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["total"], expected)
            if expected:
                self.assertIsNone(r.json()["items"][0]["device_count"])
        r = await self.web.get(f"/api/web/admin/keys/{self.kid}")
        self.assertEqual(r.json()["device_sync"]["status"], "unsupported")
        self.api.devices.assert_not_awaited()
