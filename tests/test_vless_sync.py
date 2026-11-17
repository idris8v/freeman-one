"""I04 PostgreSQL reconciliation; no real provider writes or production data."""

import asyncio
import copy
import json
import os
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_billing_concurrency import BillingConcurrency
from test_vless_creation import Panel as CreationPanel
from test_vless_renewal import REAL_CONTROL
from test_vless_renewal import Panel as RenewalPanel

from app.database.models import (
    AccessKey,
    Payment,
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
from app.database.vless_sync_migration import upgrade
from app.services.remnawave import RemnawaveApiError, RemnawaveService, RemnawaveWorker
from app.services.remnawave_bot import RemnawaveBot
from app.services.vless_sync import VlessReconciler


def complete(data):
    return {
        **data,
        "shortUuid": data.get("shortUuid", "fixture-short"),
        "subscriptionUrl": data.get(
            "subscriptionUrl", "https://sub.example.test/fixture"
        ),
        "trafficLimitBytes": data.get("trafficLimitBytes", 0),
        "updatedAt": now().isoformat(),
        "lastTrafficResetAt": None,
        "userTraffic": dict(
            usedTrafficBytes=1234, lifetimeUsedTrafficBytes=5678, onlineAt=None
        ),
    }


class Panel(RenewalPanel):
    def __init__(self, remote):
        super().__init__(remote)
        self.read_error = None
        self.read_entered = self.read_release = None
        self.health = AsyncMock(return_value={})
        self.stats = AsyncMock(
            side_effect=lambda: {
                "response": {
                    "timestamp": int(now().timestamp() * 1000),
                    "users": {"totalUsers": 1},
                    "onlineStats": {"onlineNow": 0},
                    "nodes": {"totalBytesLifetime": "0"},
                }
            }
        )
        self.users = AsyncMock(
            return_value={"response": {"total": 1, "users": [{"id": 101}]}}
        )

    async def user(self, identity):
        data = await super().user(identity)
        if self.read_entered:
            self.read_entered.set()
            await asyncio.wait_for(self.read_release.wait(), 10)
        if self.read_error is not None:
            raise RemnawaveApiError(
                "Injected read failure",
                self.read_error,
                error_code="A025" if self.read_error == 404 else None,
            )
        return data


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class VlessSynchronization(unittest.IsolatedAsyncioTestCase):
    checkout = BillingConcurrency.checkout

    async def asyncSetUp(self):
        await BillingConcurrency.asyncSetUp(self)
        self.sid, self.kid, self.initial = await BillingConcurrency.subscription(self)
        async with self.db.sessions.begin() as session:
            credential = await session.scalar(select(VlessCredential))
            credential.remote_user_uuid = "101"
            credential.short_uuid = "fixture-short"
            credential.traffic_used = 73
            self.cid, self.pid = credential.id, credential.server_id
        self.api = Panel(
            complete(
                dict(
                    id=101,
                    status="ACTIVE",
                    expireAt=self.initial.isoformat(),
                    hwidDeviceLimit=1,
                )
            )
        )
        for item in (
            patch(
                "app.services.remnawave.RemnawaveService.api",
                side_effect=lambda *_: self.api,
            ),
            patch("app.services.vless_provision.control", new=REAL_CONTROL),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.sync = VlessReconciler(self.db, self.settings, rate_seconds=0)

    async def state(self):
        async with self.db.sessions() as session:
            return (
                await session.get(VlessCredential, self.cid),
                await session.get(Subscription, self.sid),
                await session.get(AccessKey, self.kid),
                await session.get(VlessSync, f"key:{self.cid}"),
            )

    async def due(self):
        async with self.db.sessions.begin() as session:
            for row in await session.scalars(select(VlessSync)):
                row.next_attempt_at = now() - timedelta(seconds=1)

    async def expire_leases(self):
        async with self.db.sessions.begin() as session:
            for model in (VlessSync, VlessSyncGate):
                for row in await session.scalars(select(model)):
                    row.lease_until = now() - timedelta(seconds=1)
                    if isinstance(row, VlessSyncGate):
                        row.next_request_at = now() - timedelta(seconds=1)
        await self.due()

    async def reconcile(self):
        await self.due()
        await self.sync.once()

    async def assert_paid_terms(self):
        credential, sub, key, _ = await self.state()
        self.assertEqual(
            [credential.expires_at, sub.expires_at, key.expires_at], [self.initial] * 3
        )
        self.assertEqual([credential.device_limit, sub.device_limit], [1, 1])
        self.assertEqual([credential.status, key.status], ["active", "active"])
        self.assertEqual(self.api.patches, [])
        self.assertEqual(self.api.enables, [])

    async def test_worker_reconciles_individual_traffic(self):
        await RemnawaveWorker(self.db, self.settings).once()
        credential, _, _, row = await self.state()
        self.assertEqual(credential.traffic_used, 1234)
        self.assertEqual(row.outcome, "confirmed")
        self.assertIsNotNone(row.last_success_at)
        self.assertEqual(credential.last_sync, row.last_success_at)
        self.assertEqual(json.loads(row.observed)["lifetime_traffic_used"], 5678)
        await self.assert_paid_terms()

    async def test_external_changes_are_observed_without_changing_paid_terms(self):
        self.api.remote.update(
            status="LIMITED",
            expireAt=(self.initial - timedelta(days=9)).isoformat(),
            hwidDeviceLimit=7,
            trafficLimitBytes=99,
        )
        await self.reconcile()
        row = (await self.state())[3]
        self.assertEqual(
            set(json.loads(row.drift)),
            {"status", "expires_at", "device_limit", "traffic_limit"},
        )
        self.assertEqual(json.loads(row.observed)["status"], "LIMITED")
        self.assertEqual(json.loads(row.observed)["device_limit"], 7)
        self.assertEqual(
            json.loads(row.desired)["expires_at"], self.initial.isoformat()
        )
        await self.assert_paid_terms()

    async def test_longer_external_expiry_is_not_promoted_to_paid_entitlement(self):
        self.api.remote["expireAt"] = (self.initial + timedelta(days=90)).isoformat()
        await self.reconcile()
        await self.assert_paid_terms()
        self.assertIn("expires_at", json.loads((await self.state())[3].drift))

    async def test_confirmed_connection_url_updates(self):
        self.api.remote.update(
            subscriptionUrl="https://sub.example.test/new-token", shortUuid="new-token"
        )
        await self.reconcile()
        credential = (await self.state())[0]
        self.assertEqual(
            credential.subscription_url, self.api.remote["subscriptionUrl"]
        )
        self.assertEqual(credential.short_uuid, "new-token")

    async def test_null_device_limit_is_preserved_as_null_observation(self):
        self.api.remote["hwidDeviceLimit"] = None
        await self.reconcile()
        row = (await self.state())[3]
        self.assertIsNone(json.loads(row.observed)["device_limit"])
        self.assertIn("device_limit", json.loads(row.drift))
        await self.assert_paid_terms()

    async def test_real_zero_traffic_is_not_treated_as_unavailable(self):
        self.api.remote["userTraffic"]["usedTrafficBytes"] = 0
        await self.reconcile()
        self.assertEqual((await self.state())[0].traffic_used, 0)
        self.assertEqual((await self.state())[3].outcome, "confirmed")

    async def test_confirmed_404_never_deletes_or_releases_capacity(self):
        await self.reconcile()
        before = await self.state()
        self.api.read_error = 404
        await self.reconcile()
        credential, _, _, row = await self.state()
        self.assertEqual(row.outcome, "missing")
        self.assertFalse(json.loads(row.observed)["exists"])
        self.assertEqual(credential.traffic_used, before[0].traffic_used)
        self.assertEqual(credential.last_sync, before[0].last_sync)
        self.assertEqual(json.loads(row.observed)["traffic_used"], 1234)
        await self.assert_paid_terms()

    async def test_transient_failures_preserve_last_confirmed_observation(self):
        await self.reconcile()
        before = await self.state()
        for status in (0, 429, 500, 503, 401, 403):
            with self.subTest(status=status):
                self.api.read_error = status
                await self.reconcile()
                credential, _, _, row = await self.state()
                self.assertEqual(row.outcome, "unavailable")
                self.assertEqual(row.observed, before[3].observed)
                self.assertEqual(row.last_success_at, before[3].last_success_at)
                self.assertEqual(credential.traffic_used, 1234)
                self.assertGreater(row.next_attempt_at, now())

    async def test_malformed_fields_never_become_zero_or_deleted(self):
        await self.reconcile()
        original = copy.deepcopy(self.api.remote)
        invalid = [
            ("userTraffic", None),
            ("hwidDeviceLimit", -1),
            ("hwidDeviceLimit", True),
            ("expireAt", "invalid"),
            ("expireAt", "2027-01-01T12:00:00"),
            ("status", "UNKNOWN"),
            ("subscriptionUrl", "javascript:alert(1)"),
            ("subscriptionUrl", "https://user:password@sub.example.test/secret"),
            ("shortUuid", ""),
            ("updatedAt", None),
            ("id", 999),
            ("trafficLimitBytes", -1),
            ("trafficLimitBytes", 2**63),
            ("subscriptionUrl", None),
            ("subscriptionUrl", {}),
            ("subscriptionUrl", []),
        ]
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                self.api.remote = {**copy.deepcopy(original), field: value}
                await self.reconcile()
                credential, _, _, row = await self.state()
                self.assertEqual(row.outcome, "malformed")
                self.assertEqual(credential.traffic_used, 1234)
                self.assertTrue(json.loads(row.observed)["exists"])
        for field in ("usedTrafficBytes", "lifetimeUsedTrafficBytes", "onlineAt"):
            self.api.remote = copy.deepcopy(original)
            del self.api.remote["userTraffic"][field]
            await self.reconcile()
            self.assertEqual((await self.state())[3].outcome, "malformed")
        for value in (None, -1, "0", True, 2**63):
            self.api.remote = copy.deepcopy(original)
            self.api.remote["userTraffic"]["usedTrafficBytes"] = value
            await self.reconcile()
            self.assertEqual((await self.state())[0].traffic_used, 1234)
            self.assertEqual((await self.state())[3].outcome, "malformed")

    async def test_unconfirmed_gateway_404_is_not_a_missing_user(self):
        await self.reconcile()
        previous = (await self.state())[3]
        self.api.user = AsyncMock(side_effect=RemnawaveApiError("Gateway 404", 404))
        await self.reconcile()
        row = (await self.state())[3]
        self.assertEqual(row.outcome, "unavailable")
        self.assertEqual(row.observed, previous.observed)
        self.assertEqual(row.last_success_at, previous.last_success_at)

    async def test_local_traffic_reset_during_get_preserves_fresh_cache(self):
        await self.sync.seed()
        work = await self.sync.claim()
        async with self.db.sessions.begin() as session:
            (await session.get(VlessCredential, self.cid)).traffic_used = 0
        await self.sync.process(work)
        credential, _, _, row = await self.state()
        self.assertEqual(credential.traffic_used, 0)
        self.assertEqual(row.outcome, "local_changed")

    async def test_future_remote_timestamp_is_not_cached(self):
        self.api.remote["updatedAt"] = (now() + timedelta(days=1)).isoformat()
        await self.reconcile()
        credential, _, _, row = await self.state()
        self.assertEqual(credential.traffic_used, 73)
        self.assertEqual(row.outcome, "malformed")

    async def test_invalid_json_200_is_malformed_not_unavailable(self):
        self.api.read_error = 200
        await self.reconcile()
        credential, _, _, row = await self.state()
        self.assertEqual(row.outcome, "malformed")
        self.assertIsNone(row.observed)
        self.assertEqual(credential.traffic_used, 73)

    async def test_older_remote_updated_at_is_ignored(self):
        await self.reconcile()
        before = await self.state()
        self.api.remote.update(
            updatedAt=(now() - timedelta(days=1)).isoformat(), status="DISABLED"
        )
        await self.reconcile()
        self.assertEqual((await self.state())[3].outcome, "stale")
        self.assertEqual((await self.state())[3].observed, before[3].observed)
        await self.assert_paid_terms()

    async def test_traffic_decrease_requires_confirmed_reset(self):
        await self.reconcile()
        self.api.remote["userTraffic"]["usedTrafficBytes"] = 0
        await self.reconcile()
        self.assertEqual((await self.state())[3].outcome, "stale")
        self.assertEqual((await self.state())[0].traffic_used, 1234)
        self.api.remote["lastTrafficResetAt"] = now().isoformat()
        await self.reconcile()
        self.assertEqual((await self.state())[0].traffic_used, 0)
        self.assertEqual((await self.state())[3].outcome, "confirmed")

    async def test_old_data_after_404_cannot_erase_last_good_state(self):
        await self.reconcile()
        self.api.read_error = 404
        await self.reconcile()
        self.api.read_error = None
        self.api.remote["updatedAt"] = (now() - timedelta(days=1)).isoformat()
        await self.reconcile()
        row = (await self.state())[3]
        self.assertEqual(row.outcome, "stale")
        self.assertFalse(json.loads(row.observed)["exists"])

    async def test_periodic_sync_skips_fresh_records_across_restart(self):
        await self.reconcile()
        calls = len(self.api.lookups)
        self.sync = VlessReconciler(self.db, self.settings, rate_seconds=0)
        await self.db.engine.dispose()
        await self.sync.once()
        self.assertEqual(len(self.api.lookups), calls)
        async with self.db.sessions() as session:
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(VlessSync)), 1
            )

    async def test_concurrent_workers_read_same_key_once_without_http_locks(self):
        self.api.read_entered, self.api.read_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.sync.once())
        try:
            await asyncio.wait_for(self.api.read_entered.wait(), 5)
            await asyncio.wait_for(
                VlessReconciler(self.db, self.settings, rate_seconds=0).once(), 3
            )
            self.assertEqual(len(self.api.lookups), 1)
            async with self.db.sessions.begin() as session:
                for model in (
                    VlessSync,
                    VlessSyncGate,
                    RemnawaveServer,
                    Subscription,
                    AccessKey,
                    VlessCredential,
                ):
                    self.assertIsNotNone(
                        await session.scalar(select(model).with_for_update(nowait=True))
                    )
        finally:
            self.api.read_release.set()
            await asyncio.wait_for(task, 5)
        self.assertEqual((await self.state())[3].outcome, "confirmed")

    async def test_local_change_while_get_in_flight_discards_snapshot(self):
        await self.sync.seed()
        work = await self.sync.claim()
        async with self.db.sessions.begin() as session:
            for model, identity in (
                (Subscription, self.sid),
                (AccessKey, self.kid),
                (VlessCredential, self.cid),
            ):
                row = await session.get(model, identity)
                row.expires_at += timedelta(days=30)
        await self.sync.process(work)
        credential, sub, key, row = await self.state()
        self.assertEqual(row.outcome, "local_changed")
        self.assertIsNone(row.last_success_at)
        self.assertEqual(
            [credential.expires_at, sub.expires_at, key.expires_at],
            [self.initial + timedelta(days=30)] * 3,
        )
        self.assertEqual(credential.traffic_used, 73)

    async def test_concurrent_local_ownership_change_discards_snapshot(self):
        await self.sync.seed()
        work = await self.sync.claim()
        async with self.db.sessions.begin() as session:
            for model, identity in (
                (Subscription, self.sid),
                (AccessKey, self.kid),
                (VlessCredential, self.cid),
            ):
                (await session.get(model, identity)).user_id = self.other
        await self.sync.process(work)
        credential, _, _, row = await self.state()
        self.assertEqual(row.outcome, "local_changed")
        self.assertEqual(credential.user_id, self.other)
        self.assertEqual(credential.traffic_used, 73)
        self.assertIsNone(row.last_success_at)

    async def test_renewal_lock_does_not_block_reconciliation_worker(self):
        await self.sync.seed()
        work = await self.sync.claim()
        async with self.db.sessions.begin() as blocker:
            await blocker.get(Subscription, self.sid, with_for_update=True)
            await asyncio.wait_for(self.sync.process(work), 3)
        row = (await self.state())[3]
        self.assertEqual(row.outcome, "local_busy")
        self.assertIsNone(row.last_success_at)
        await self.reconcile()
        self.assertEqual((await self.state())[3].outcome, "confirmed")

    async def test_cancellation_restart_releases_only_dispatch_lease(self):
        self.api.read_entered, self.api.read_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.sync.once())
        await asyncio.wait_for(self.api.read_entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue((await self.state())[3].lease_token)
        await self.expire_leases()
        self.api.read_entered = None
        self.sync = VlessReconciler(self.db, self.settings, rate_seconds=0)
        await self.sync.once()
        self.assertEqual((await self.state())[3].outcome, "confirmed")

    async def test_expired_worker_cannot_overwrite_newer_observation(self):
        await self.sync.seed()
        old = await self.sync.claim()
        await self.expire_leases()
        new = await self.sync.claim()
        self.assertNotEqual(old.token, new.token)
        await self.sync.process(new)
        before = await self.state()
        self.assertFalse(await self.sync.finish(old, "missing", dict(exists=False)))
        self.assertEqual((await self.state())[3].observed, before[3].observed)

    async def test_failed_local_commit_recovers_after_restart(self):
        await self.sync.seed()
        work = await self.sync.claim()
        armed = True

        def fail(session):
            nonlocal armed
            if armed and any(
                isinstance(row, VlessSync) and row.outcome == "confirmed"
                for row in session.dirty
            ):
                armed = False
                raise SQLAlchemyError("Injected I04 commit failure")

        event.listen(Session, "before_commit", fail)
        try:
            with self.assertRaises(SQLAlchemyError):
                await self.sync.process(work)
        finally:
            event.remove(Session, "before_commit", fail)
        self.assertFalse(armed)
        credential, _, _, row = await self.state()
        self.assertEqual(credential.traffic_used, 73)
        self.assertIsNone(row.observed)
        self.assertTrue(row.lease_token)
        await self.expire_leases()
        await self.db.engine.dispose()
        self.sync = VlessReconciler(self.db, self.settings, rate_seconds=0)
        await self.sync.once()
        self.assertEqual((await self.state())[0].traffic_used, 1234)
        await self.assert_paid_terms()

    async def test_rate_gate_is_durable_across_workers(self):
        self.sync.rate_seconds = 10
        await self.sync.seed()
        await self.sync.process(await self.sync.claim())
        await self.due()
        self.assertIsNone(await VlessReconciler(self.db, self.settings).claim())
        self.assertEqual(len(self.api.lookups), 1)
        async with self.db.sessions() as session:
            gate = await session.get(VlessSyncGate, self.pid)
            self.assertGreater(gate.next_request_at, now())

    async def test_batches_are_bounded_and_progress_incrementally(self):
        async with self.db.sessions.begin() as session:
            for _ in range(7):
                sub = Subscription(user_id=self.uid, expires_at=self.initial)
                key = AccessKey(
                    user_id=self.uid,
                    name="fixture",
                    mode="vless",
                    expires_at=self.initial,
                )
                session.add_all([sub, key])
                await session.flush()
                session.add(
                    VlessCredential(
                        server_id=self.pid,
                        user_id=self.uid,
                        subscription_id=sub.id,
                        access_key_id=key.id,
                        remote_user_uuid=str(102 + sub.id),
                        expires_at=self.initial,
                    )
                )
        self.api.user = AsyncMock(side_effect=RemnawaveApiError("missing", 404))
        await self.sync.once(limit=3)
        self.assertLessEqual(self.api.user.await_count, 3)
        async with self.db.sessions() as session:
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(VlessSync)), 3
            )
        await self.sync.once(limit=3)
        async with self.db.sessions() as session:
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(VlessSync)), 6
            )

    async def test_manual_telegram_refresh_uses_validated_nested_traffic(self):
        with patch.object(RemnawaveService, "admin"):
            async with self.db.sessions.begin() as session:
                screen = await RemnawaveBot(
                    SQLRepository(session), self.settings
                ).render(f"admin:r:refresh:{self.cid}", self.user)
                self.assertIn("Трафик:", screen.text)
        self.assertEqual((await self.state())[0].traffic_used, 1234)

    async def test_manual_refresh_with_one_request_pool_connection(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        url = self.db.engine.url
        await self.db.engine.dispose()
        self.db.engine = create_async_engine(
            url,
            pool_size=1,
            max_overflow=0,
            pool_timeout=0.5,
            connect_args={"server_settings": {"search_path": self.pg_schema}},
        )
        self.db.sessions = async_sessionmaker(self.db.engine, expire_on_commit=False)
        with patch.object(RemnawaveService, "admin"):
            async with self.db.sessions.begin() as session:
                await asyncio.wait_for(
                    RemnawaveBot(SQLRepository(session), self.settings).render(
                        f"admin:r:refresh:{self.cid}", self.user
                    ),
                    10,
                )
        self.assertEqual((await self.state())[0].traffic_used, 1234)

    async def test_manual_malformed_refresh_keeps_previous_traffic(self):
        self.api.remote["userTraffic"]["usedTrafficBytes"] = None
        with patch.object(RemnawaveService, "admin"):
            with self.assertRaises(ValueError):
                async with self.db.sessions.begin() as session:
                    await RemnawaveBot(SQLRepository(session), self.settings).render(
                        f"admin:r:refresh:{self.cid}", self.user
                    )
        self.assertEqual((await self.state())[0].traffic_used, 73)
        self.assertEqual((await self.state())[3].outcome, "malformed")

    async def test_pending_i02_is_observed_and_original_retry_recovers(self):
        async with self.db.sessions.begin() as session:
            credential = await session.get(VlessCredential, self.cid)
            key = await session.get(AccessKey, self.kid)
            credential.status, key.status = "disabled", "billing_blocked"
            await SQLRepository(session).set_setting(f"billing_blocked:{self.kid}", "1")
        self.api.remote["status"] = "DISABLED"
        self.api.enable_error = 503
        first = await self.checkout(self.sid, 3, "i04-pending-renewal")
        self.assertEqual(first.status_code, 409, first.text)
        async with self.db.sessions() as session:
            operation = await session.scalar(select(VlessRenewal))
            self.assertIsNotNone(operation, first.text)
            before = operation.state, operation.patch_state, operation.enable_state
            target = operation.expires_at
        await self.reconcile()
        credential, sub, _, row = await self.state()
        self.assertEqual(credential.expires_at, self.initial)
        self.assertEqual(sub.expires_at, self.initial)
        self.assertEqual(credential.traffic_used, 73)
        self.assertIn("pending_renewal", json.loads(row.drift))
        self.assertEqual(json.loads(row.observed)["expires_at"], target.isoformat())
        async with self.db.sessions() as session:
            operation = await session.scalar(select(VlessRenewal))
            self.assertEqual(
                (operation.state, operation.patch_state, operation.enable_state), before
            )
        self.api.enable_error = None
        self.assertEqual(
            (await self.checkout(self.sid, 3, "i04-pending-renewal")).status_code, 200
        )
        self.assertEqual(len(self.api.patches), 1)
        await self.reconcile()
        credential, sub, key, row = await self.state()
        self.assertEqual(
            [credential.expires_at, sub.expires_at, key.expires_at], [target] * 3
        )
        self.assertEqual(credential.device_limit, 3)
        self.assertNotIn("pending_renewal", json.loads(row.drift))
        async with self.db.sessions() as session:
            self.assertEqual(await SQLRepository(session).balance(self.uid), 70_000)
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(Payment)), 1
            )

    async def orphan(self, *, missing=False):
        self.api = CreationPanel()
        self.api.lose_response = not missing
        self.api.creation_error = 503 if missing else None
        result = await self.checkout(None, 1, "i04-orphan-checkout")
        self.assertEqual(result.status_code, 409, result.text)
        self.api.lose_response = self.api.lookup_error = False
        original = self.api.user_by_username

        async def lookup(username):
            try:
                return await original(username)
            except RemnawaveApiError as error:
                if error.status == 404:
                    error.error_code = "A025"
                raise

        self.api.user_by_username = lookup
        # Add the full real 3.4.4 response envelope to the existing I01 fixture.
        for username, data in self.api.remote.items():
            self.api.remote[username] = {**data, **complete(data)}
        await self.sync.seed()
        async with self.db.sessions() as session:
            operation = await session.scalar(select(VlessCreation))
        work = await self.sync.claim(f"creation:{operation.id}")
        self.assertIsNotNone(work)
        return operation, work

    async def test_unknown_i01_read_only_recovery_consumes_existing_reservation(self):
        operation, work = await self.orphan()
        await self.sync.process(work)
        async with self.db.sessions() as session:
            row = await session.get(VlessCreation, operation.id)
            self.assertEqual(row.state, "remote_confirmed")
            self.assertIsNone(row.credential_id)
            self.assertEqual(
                (await session.get(VlessReservation, row.id)).state, "consumed"
            )
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(VlessCredential)),
                1,
            )
            self.assertEqual(await SQLRepository(session).balance(self.uid), 100_000)
        self.assertEqual(len(self.api.creates), 1)
        result = await self.checkout(None, 1, "i04-orphan-checkout")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(self.api.creates), 1)
        async with self.db.sessions() as session:
            self.assertEqual(
                (await session.get(VlessCreation, operation.id)).state, "associated"
            )
            self.assertEqual(
                (await session.get(VlessReservation, operation.id)).state, "consumed"
            )

    async def test_i01_external_term_conflict_is_observed_without_activation(self):
        operation, work = await self.orphan()
        for data in self.api.remote.values():
            data["expireAt"] = (now() - timedelta(days=1)).isoformat()
        await self.sync.process(work)
        async with self.db.sessions() as session:
            row = await session.get(VlessSync, work.id)
            self.assertEqual(row.outcome, "creation_conflict")
            self.assertIsNotNone(row.last_success_at)
            self.assertIn("creation_terms", json.loads(row.drift))
            self.assertEqual(
                (await session.get(VlessCreation, operation.id)).state,
                "outcome_unknown",
            )
            self.assertEqual(
                (await session.get(VlessReservation, operation.id)).state, "reserved"
            )
            self.assertEqual(await SQLRepository(session).balance(self.uid), 100_000)
        self.assertEqual(len(self.api.creates), 1)

    async def test_i01_confirmation_commit_failure_retains_capacity_and_recovers(self):
        operation, work = await self.orphan()
        armed = True

        def fail(session):
            nonlocal armed
            if armed and any(
                isinstance(row, VlessCreation) and row.state == "remote_confirmed"
                for row in session.dirty
            ):
                armed = False
                raise SQLAlchemyError("Injected I01 reconciliation commit failure")

        event.listen(Session, "before_commit", fail)
        try:
            with self.assertRaises(SQLAlchemyError):
                await self.sync.process(work)
        finally:
            event.remove(Session, "before_commit", fail)
        self.assertFalse(armed)
        async with self.db.sessions() as session:
            row = await session.get(VlessReservation, operation.id)
            self.assertEqual(row.state, "reserved")
            self.assertFalse(row.lease_token)
        await self.expire_leases()
        self.sync = VlessReconciler(self.db, self.settings, rate_seconds=0)
        await self.sync.process(await self.sync.claim(work.id))
        async with self.db.sessions() as session:
            self.assertEqual(
                (await session.get(VlessReservation, operation.id)).state, "consumed"
            )
            self.assertEqual(
                (await session.get(VlessCreation, operation.id)).state,
                "remote_confirmed",
            )
        self.assertEqual(len(self.api.creates), 1)

    async def test_invalid_panel_credentials_are_unavailable_not_zero(self):
        from cryptography.fernet import InvalidToken

        with patch.object(RemnawaveService, "api", side_effect=InvalidToken):
            await self.reconcile()
        credential, _, _, row = await self.state()
        self.assertEqual(row.outcome, "unavailable")
        self.assertIsNone(row.observed)
        self.assertEqual(credential.traffic_used, 73)
        self.assertFalse(row.lease_token)

    async def test_unknown_i01_404_keeps_reservation_without_recreation(self):
        operation, work = await self.orphan(missing=True)
        await self.sync.process(work)
        async with self.db.sessions() as session:
            self.assertEqual(
                (await session.get(VlessCreation, operation.id)).state,
                "outcome_unknown",
            )
            self.assertEqual(
                (await session.get(VlessReservation, operation.id)).state, "reserved"
            )
            self.assertEqual((await session.get(VlessSync, work.id)).outcome, "missing")
        self.assertEqual(len(self.api.creates), 1)

    async def test_deleted_key_is_retired_without_remote_request(self):
        await self.sync.seed()
        async with self.db.sessions.begin() as session:
            (await session.get(VlessCredential, self.cid)).status = "deleted"
        await self.sync.once()
        self.assertEqual(self.api.lookups, [])
        self.assertEqual((await self.state())[3].outcome, "retired")

    async def test_migration_is_additive_repeatable_and_preserves_keys(self):
        before = await self.state()
        async with self.db.engine.begin() as connection:
            for model in (VlessSync, VlessSyncGate):
                await connection.run_sync(model.__table__.drop)
        await upgrade(self.db.engine)
        await upgrade(self.db.engine)
        after = await self.state()
        self.assertEqual(after[0].remote_user_uuid, before[0].remote_user_uuid)
        self.assertEqual(after[0].traffic_used, before[0].traffic_used)
        self.assertEqual(after[1].expires_at, before[1].expires_at)
        await self.reconcile()
        self.assertEqual((await self.state())[3].outcome, "confirmed")


class RemnawaveReadContract(unittest.IsolatedAsyncioTestCase):
    async def error(self, payload):
        from app.services.remnawave import RemnawaveApiClient

        class Response:
            status, headers = 404, {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def json(self, **_):
                if isinstance(payload, Exception):
                    raise payload
                return payload

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            def request(self, *_, **__):
                return Response()

        with patch(
            "app.services.remnawave.aiohttp.ClientSession", return_value=Client()
        ):
            with self.assertRaises(RemnawaveApiError) as caught:
                await RemnawaveApiClient("https://panel.example.test", "fixture").user(
                    "101"
                )
        return caught.exception

    async def test_documented_missing_user_codes_are_preserved(self):
        for code in ("A025", "A063"):
            error = await self.error(dict(errorCode=code, message="User not found"))
            self.assertEqual((error.status, error.error_code), (404, code))

    async def test_gateway_html_404_does_not_confirm_user_deletion(self):
        error = await self.error(ValueError("HTML, not JSON"))
        self.assertEqual(error.status, 404)
        self.assertIsNone(error.error_code)

    async def test_generic_404_does_not_invent_provider_error_code(self):
        error = await self.error(dict(statusCode=404, message="Not Found"))
        self.assertIsNone(error.error_code)

    async def test_malformed_error_codes_remain_unconfirmed(self):
        for code in ([], {"unexpected": "payload"}, True, 25):
            error = await self.error(dict(errorCode=code))
            self.assertEqual(error.status, 404)
            self.assertIsNone(error.error_code)
