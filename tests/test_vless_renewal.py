"""PostgreSQL renewal faults; RemnaWave and payment responses are fixture-only."""

import asyncio
import copy
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from test_billing_concurrency import NOW, BillingConcurrency

from app.database.models import (
    AccessKey,
    Payment,
    Subscription,
    User,
    VlessCredential,
    VlessRenewal,
)
from app.database.repositories import SQLRepository
from app.database.vless_renewal_migration import upgrade
from app.services import vless_provision
from app.services.admin import AdminService
from app.services.billing import BillingService, prepare_autorenew
from app.services.remnawave import RemnawaveApiError
from app.services.remnawave_bot import RemnawaveBot
from app.services.vless_renewal import renew
from app.web.admin_dashboard import admin_dashboard_router

REAL_CONTROL = vless_provision.control
UTC = timezone.utc


class Panel:
    def __init__(self, remote):
        self.remote = remote
        self.remote.setdefault("trafficLimitBytes", 0)  # Required 3.4.4 GET field.
        self.patches, self.enables, self.lookups = [], [], []
        self.patch_error = self.enable_error = None
        self.lose_patch = self.lose_enable = False
        self.outage = self.outage_after_write = False
        self.ignore_patch = self.ignore_devices = False
        self.entered = self.release = self.before_enable = None

    async def user(self, identity):
        self.lookups.append(identity)
        if self.outage:
            raise RemnawaveApiError("Injected lookup outage", 503)
        return {"response": copy.deepcopy(self.remote)}

    async def update_user(self, payload):
        self.patches.append(copy.deepcopy(payload))
        if self.patch_error:
            raise RemnawaveApiError("Injected PATCH rejection", self.patch_error)
        if not self.ignore_patch:
            self.remote["expireAt"] = payload["expireAt"]
            if not self.ignore_devices:
                self.remote["hwidDeviceLimit"] = payload["hwidDeviceLimit"]
            if "status" in payload:
                self.remote["status"] = payload["status"]
            if "trafficLimitBytes" in payload:
                self.remote["trafficLimitBytes"] = payload["trafficLimitBytes"]
        if self.entered:
            self.entered.set()
            await asyncio.wait_for(self.release.wait(), 15)
        if self.lose_patch:
            self.outage = self.outage_after_write
            raise RemnawaveApiError("Injected PATCH response loss")
        return {"response": copy.deepcopy(self.remote)}

    async def enable_user(self, identity):
        self.enables.append(identity)
        if self.before_enable:
            await self.before_enable()
        if self.enable_error:
            raise RemnawaveApiError("Injected enable rejection", self.enable_error)
        self.remote["status"] = "ACTIVE"
        if self.lose_enable:
            self.outage = self.outage_after_write
            raise RemnawaveApiError("Injected enable response loss")
        return {"response": copy.deepcopy(self.remote)}


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class VlessRenewalConsistency(unittest.IsolatedAsyncioTestCase):
    checkout = BillingConcurrency.checkout
    paid_order = BillingConcurrency.paid_order
    receipt = BillingConcurrency.receipt

    async def asyncSetUp(self):
        await BillingConcurrency.asyncSetUp(self)
        # The original C01 fixture isolates HTTP at control(). These integration
        # tests use the real control and fake only the RemnaWave adapter.
        control = patch("app.services.vless_provision.control", new=REAL_CONTROL)
        control.start()
        self.addCleanup(control.stop)
        self.sid, self.kid, self.initial = await BillingConcurrency.subscription(
            self, expired=True
        )
        async with self.db.sessions.begin() as session:
            credential = await session.scalar(select(VlessCredential))
            credential.remote_user_uuid = "101"
            self.cid, self.pid = credential.id, credential.server_id
        self.api = Panel(
            dict(
                id=101,
                expireAt=self.initial.isoformat(),
                hwidDeviceLimit=1,
                status="DISABLED",
            )
        )
        api = patch(
            "app.services.remnawave.RemnawaveService.api",
            side_effect=lambda *_: self.api,
        )
        api.start()
        self.addCleanup(api.stop)

    async def operations(self):
        async with self.db.sessions() as session:
            return list(
                await session.scalars(
                    select(VlessRenewal).order_by(VlessRenewal.created_at)
                )
            )

    async def assert_local(
        self, expiry=None, devices=1, payments=0, debit=0, active=False
    ):
        expiry = expiry or self.initial
        async with self.db.sessions() as session:
            sub = await session.get(Subscription, self.sid)
            key = await session.get(AccessKey, self.kid)
            credential = await session.get(VlessCredential, self.cid)
            self.assertEqual(
                [sub.expires_at, key.expires_at, credential.expires_at], [expiry] * 3
            )
            self.assertEqual([sub.device_limit, credential.device_limit], [devices] * 2)
            self.assertEqual(credential.status, "active" if active else "disabled")
            self.assertEqual(key.status, "active" if active else "billing_blocked")
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(Payment)),
                payments,
            )
            self.assertEqual(
                await SQLRepository(session).balance(self.uid), 100_000 - debit
            )
            self.assertEqual(
                await SQLRepository(session).setting(f"billing_blocked:{self.kid}"),
                "0" if active else "1",
            )

    async def successful(self, key, months=1):
        result = await self.checkout(self.sid, months, key)
        self.assertEqual(result.status_code, 200, result.text)
        return result

    async def test_patch_success_failed_enable_recovers_original_period_and_limits(
        self,
    ):
        key = "partial-renewal-enable"
        self.api.enable_error = 503
        first = await self.checkout(self.sid, 3, key)
        self.assertEqual(first.status_code, 409, first.text)
        await self.assert_local()
        operation = (await self.operations())[0]
        self.assertEqual(
            (operation.state, operation.patch_state, operation.enable_state),
            ("patch_confirmed", "confirmed", "outcome_unknown"),
        )
        self.assertEqual(self.api.remote["hwidDeviceLimit"], 3)
        self.assertEqual(self.api.remote["status"], "DISABLED")
        self.api.enable_error = None
        with patch("app.services.billing.now", return_value=NOW + timedelta(hours=3)):
            result = await self.successful(key, 3)
        self.assertEqual((await self.successful(key, 3)).json(), result.json())
        await self.assert_local(
            datetime(2027, 4, 30, 12, tzinfo=UTC), 3, 1, 30_000, True
        )
        self.assertEqual(len(self.api.patches), 1)
        self.assertEqual(len(self.api.enables), 2)
        self.assertEqual((await self.operations())[0].state, "applied")

    async def test_patch_progress_is_durable_before_enable(self):
        async def check():
            op = (await self.operations())[0]
            self.assertEqual(op.patch_state, "confirmed")
            self.assertEqual(op.state, "patch_confirmed")
            self.assertEqual(op.enable_state, "outcome_unknown")
            await self.assert_local()

        self.api.before_enable = check
        await self.successful("durable-patch-progress")

    async def test_lost_patch_response_is_verified_without_second_patch(self):
        self.api.lose_patch = True
        await self.successful("lost-patch-with-lookup")
        self.assertEqual(len(self.api.patches), 1)
        await self.assert_local(
            datetime(2027, 2, 28, 12, tzinfo=UTC), 1, 1, 10_000, True
        )

    async def test_lost_patch_and_lookup_outage_recover_without_repatch(self):
        self.api.lose_patch = self.api.outage_after_write = True
        key = "lost-patch-and-lookup"
        self.assertEqual((await self.checkout(self.sid, 1, key)).status_code, 409)
        await self.assert_local()
        op = (await self.operations())[0]
        self.assertEqual(
            (op.state, op.patch_state), ("outcome_unknown", "outcome_unknown")
        )
        self.api.outage = self.api.lose_patch = False
        await self.successful(key)
        self.assertEqual(len(self.api.patches), 1)

    async def test_lost_enable_response_is_verified_as_success(self):
        self.api.lose_enable = True
        await self.successful("lost-enable-with-lookup")
        self.assertEqual(len(self.api.enables), 1)
        await self.assert_local(
            datetime(2027, 2, 28, 12, tzinfo=UTC), 1, 1, 10_000, True
        )

    async def test_lost_enable_and_lookup_outage_recover_without_reenable(self):
        self.api.lose_enable = self.api.outage_after_write = True
        key = "lost-enable-and-lookup"
        self.assertEqual((await self.checkout(self.sid, 1, key)).status_code, 409)
        await self.assert_local()
        op = (await self.operations())[0]
        self.assertEqual(
            (op.patch_state, op.enable_state), ("confirmed", "outcome_unknown")
        )
        self.api.outage = self.api.lose_enable = False
        await self.successful(key)
        self.assertEqual((len(self.api.patches), len(self.api.enables)), (1, 1))

    async def test_confirmed_enable_rejection_keeps_partial_patch_progress(self):
        self.api.enable_error = 401
        key = "enable-confirmed-rejection"
        self.assertEqual((await self.checkout(self.sid, 1, key)).status_code, 409)
        op = (await self.operations())[0]
        self.assertEqual((op.state, op.enable_state), ("patch_confirmed", "failed"))
        self.api.enable_error = None
        await self.successful(key)
        self.assertEqual(len(self.api.patches), 1)

    async def test_confirmed_patch_rejection_can_rebase_after_independent_renewal(self):
        self.api.patch_error = 400
        key = "patch-confirmed-rejection"
        self.assertEqual((await self.checkout(self.sid, 1, key)).status_code, 409)
        self.assertEqual((await self.operations())[0].state, "failed")
        await self.assert_local()
        self.api.patch_error = None
        await self.successful("independent-after-rejection", 3)
        await self.successful(key)
        await self.assert_local(
            datetime(2027, 5, 30, 12, tzinfo=UTC), 1, 2, 40_000, True
        )

    async def test_unknown_unapplied_patch_is_not_blindly_dispatched_again(self):
        self.api.ignore_patch = self.api.lose_patch = True
        key = "unknown-unapplied-patch"
        self.assertEqual((await self.checkout(self.sid, 1, key)).status_code, 409)
        self.api.ignore_patch = self.api.lose_patch = False
        self.assertEqual((await self.checkout(self.sid, 1, key)).status_code, 409)
        self.assertEqual(len(self.api.patches), 1)
        self.assertEqual(self.api.enables, [])
        await self.assert_local()

    async def test_late_patch_completion_is_recovered_before_next_paid_period(self):
        key = "late-patch-original-renewal"
        self.api.ignore_patch = self.api.lose_patch = True
        self.assertEqual((await self.checkout(self.sid, 1, key)).status_code, 409)
        self.assertEqual(
            (await self.checkout(self.sid, 3, "late-patch-next-renewal")).status_code,
            409,
        )
        # The original in-flight write finally commits in the remote provider.
        original = self.api.patches[0]
        self.api.remote.update(
            expireAt=original["expireAt"], hwidDeviceLimit=original["hwidDeviceLimit"]
        )
        self.api.ignore_patch = self.api.lose_patch = False
        await self.successful(key)
        await self.successful("late-patch-next-renewal", 3)
        self.assertEqual(len(self.api.patches), 2)
        await self.assert_local(
            datetime(2027, 5, 28, 12, tzinfo=UTC), 3, 2, 40_000, True
        )

    async def test_replay_after_later_renewal_does_not_restore_old_expiry_or_limits(
        self,
    ):
        key = "old-operation-after-next-renewal"
        await self.successful(key)
        await self.successful("later-independent-renewal", 3)
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            service = BillingService(repo, self.settings)
            sub, access = await service.valid(self.user, self.sid)
            await service.fulfill(
                self.user,
                await repo.tariff(1),
                sub,
                access,
                operation_key=f"balance:web:{self.uid}:{key}",
            )
        self.assertEqual(len(self.api.patches), 2)
        await self.assert_local(
            datetime(2027, 5, 28, 12, tzinfo=UTC), 3, 2, 40_000, True
        )

    async def test_intentionally_blocked_admin_extension_preserves_disabled_status(
        self,
    ):
        expiry = NOW + timedelta(days=30)
        async with self.db.sessions.begin() as session:
            await renew(
                SQLRepository(session),
                self.settings,
                await session.get(VlessCredential, self.cid),
                expiry,
                operation_key="blocked-admin-extension",
                request=dict(days=30),
                devices=1,
                activate=False,
            )
        await self.assert_local(expiry)
        self.assertEqual(self.api.remote["status"], "DISABLED")
        self.assertEqual(self.api.patches[0]["status"], "DISABLED")
        self.assertEqual(self.api.enables, [])

    async def test_lookup_outage_before_write_is_safely_retryable(self):
        self.api.outage = True
        key = "renewal-lookup-outage"
        self.assertEqual((await self.checkout(self.sid, 1, key)).status_code, 409)
        self.assertEqual(self.api.patches, [])
        self.assertEqual((await self.operations())[0].patch_state, "not_started")
        self.api.outage = False
        await self.successful(key)

    async def test_remote_expiry_or_identity_drift_is_not_overwritten(self):
        self.api.remote["expireAt"] = datetime(2030, 1, 1, tzinfo=UTC).isoformat()
        self.assertEqual(
            (await self.checkout(self.sid, 1, "renewal-expiry-drift")).status_code, 409
        )
        self.assertEqual(self.api.patches, [])
        await self.assert_local()
        self.api.remote["id"] = 999
        self.assertEqual(
            (await self.checkout(self.sid, 1, "renewal-expiry-drift")).status_code, 409
        )
        self.assertEqual(self.api.enables, [])

    async def test_partial_device_update_is_not_reported_as_success(self):
        self.api.ignore_devices = True
        key = "partial-device-limit-patch"
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 409)
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 409)
        self.assertEqual(len(self.api.patches), 1)
        self.assertEqual(self.api.enables, [])
        await self.assert_local()

    async def test_actual_remote_status_is_used_instead_of_cached_active_status(self):
        async with self.db.sessions.begin() as session:
            (await session.get(VlessCredential, self.cid)).status = "active"
        await self.successful("stale-local-active-status")
        self.assertEqual(len(self.api.enables), 1)

    async def test_status_only_expiry_sync_cannot_overwrite_pending_paid_patch(self):
        key = "pending-renewal-status-sync"
        self.api.enable_error = 503
        self.assertEqual((await self.checkout(self.sid, 1, key)).status_code, 409)
        with self.assertRaises(ValueError):
            async with self.db.sessions.begin() as session:
                await REAL_CONTROL(
                    SQLRepository(session),
                    self.settings,
                    await session.get(VlessCredential, self.cid),
                    "extend",
                    self.initial,
                )
        self.assertEqual(len(self.api.patches), 1)
        self.api.enable_error = None
        await self.successful(key)

    async def test_stale_status_only_expiry_sync_cannot_undo_completed_renewal(self):
        await self.successful("renewal-before-stale-status-sync")
        with self.assertRaises(ValueError):
            async with self.db.sessions.begin() as session:
                await REAL_CONTROL(
                    SQLRepository(session),
                    self.settings,
                    await session.get(VlessCredential, self.cid),
                    "extend",
                    self.initial,
                )
        self.assertEqual(len(self.api.patches), 1)
        await self.assert_local(
            datetime(2027, 2, 28, 12, tzinfo=UTC), 1, 1, 10_000, True
        )
        # A fresh, settled status sync remains compatible with existing callers.
        async with self.db.sessions.begin() as session:
            await REAL_CONTROL(
                SQLRepository(session),
                self.settings,
                await session.get(VlessCredential, self.cid),
                "extend",
                datetime(2027, 2, 28, 12, tzinfo=UTC),
            )
        self.assertEqual(len(self.api.patches), 2)

    async def test_local_commit_failure_preserves_remote_confirmation_for_retry(self):
        def fail(session):
            if session.info.pop("renewal_fail_commit", False):
                raise SQLAlchemyError("Injected paid transaction commit failure")

        async def control(repo, *args, **kwargs):
            result = await REAL_CONTROL(repo, *args, **kwargs)
            repo.session.info["renewal_fail_commit"] = True
            return result

        event.listen(Session, "before_commit", fail)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=self.client._transport.app, raise_app_exceptions=False
            ),
            base_url=self.settings.web_base_url,
        )
        key = "renewal-local-commit-failure"
        try:
            with patch("app.services.vless_provision.control", side_effect=control):
                first = await client.post(
                    "/api/web/checkout",
                    json=dict(
                        subscription_id=self.sid,
                        months=1,
                        method="balance",
                        idempotency_key=key,
                    ),
                )
            self.assertEqual(first.status_code, 500)
        finally:
            event.remove(Session, "before_commit", fail)
            await client.aclose()
        await self.assert_local()
        self.assertEqual((await self.operations())[0].state, "remote_confirmed")
        await self.successful(key)
        self.assertEqual((len(self.api.patches), len(self.api.enables)), (1, 1))
        await self.assert_local(
            datetime(2027, 2, 28, 12, tzinfo=UTC), 1, 1, 10_000, True
        )

    async def journal_commit_failure(self, stage):
        armed = True

        def fail(session):
            nonlocal armed
            if armed and any(
                isinstance(row, VlessRenewal) and row.state == stage
                for row in session.dirty
            ):
                armed = False
                raise SQLAlchemyError("Injected journal result commit failure")

        event.listen(Session, "before_commit", fail)
        key = "renewal-journal-failure-" + stage
        try:
            self.assertEqual((await self.checkout(self.sid, 1, key)).status_code, 409)
        finally:
            event.remove(Session, "before_commit", fail)
        self.assertFalse(armed)
        await self.assert_local()
        await self.successful(key)
        self.assertEqual((len(self.api.patches), len(self.api.enables)), (1, 1))

    async def test_patch_confirmation_commit_failure_recovers_by_get(self):
        await self.journal_commit_failure("patch_confirmed")

    async def test_enable_confirmation_commit_failure_recovers_by_get(self):
        await self.journal_commit_failure("remote_confirmed")

    async def test_restart_recovers_partial_renewal_using_only_database_and_remote_state(
        self,
    ):
        key = "renewal-process-restart"
        self.api.enable_error = 503
        self.assertEqual((await self.checkout(self.sid, 1, key)).status_code, 409)
        self.api = Panel(self.api.remote)
        await self.db.engine.dispose()
        await self.successful(key)
        self.assertEqual(self.api.patches, [])
        await self.assert_local(
            datetime(2027, 2, 28, 12, tzinfo=UTC), 1, 1, 10_000, True
        )

    async def concurrent_periods(self, months):
        self.api.entered, self.api.release = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(
            self.checkout(self.sid, months[0], "renewal-order-first")
        )
        try:
            await asyncio.wait_for(self.api.entered.wait(), 15)
            second = asyncio.create_task(
                self.checkout(self.sid, months[1], "renewal-order-second")
            )
            await asyncio.sleep(0.1)
            self.assertFalse(second.done())
        finally:
            self.api.release.set()
        responses = await asyncio.wait_for(asyncio.gather(first, second), 30)
        self.assertEqual(
            [r.status_code for r in responses], [200, 200], [r.text for r in responses]
        )
        expiry = datetime(2027, 5, 28 if months[0] == 1 else 30, 12, tzinfo=UTC)
        await self.assert_local(expiry, months[-1], 2, 40_000, True)
        self.assertEqual(datetime.fromisoformat(self.api.remote["expireAt"]), expiry)
        self.assertEqual(len(self.api.patches), 2)
        self.assertEqual(len(self.api.enables), 1)

    async def test_concurrent_expired_renewals_one_then_three_months(self):
        await self.concurrent_periods((1, 3))

    async def test_concurrent_expired_renewals_three_then_one_month(self):
        await self.concurrent_periods((3, 1))

    async def test_concurrent_duplicate_checkout_extends_and_charges_once(self):
        responses = await asyncio.wait_for(
            asyncio.gather(
                self.checkout(self.sid, 1, "duplicate-remote-renewal"),
                self.checkout(self.sid, 1, "duplicate-remote-renewal"),
            ),
            30,
        )
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(responses[0].json(), responses[1].json())
        self.assertEqual(len(self.api.patches), 1)
        await self.assert_local(
            datetime(2027, 2, 28, 12, tzinfo=UTC), 1, 1, 10_000, True
        )

    async def test_partial_paid_receipt_fences_second_receipt_until_original_recovers(
        self,
    ):
        first, second = (
            await self.paid_order(self.sid, 1),
            await self.paid_order(self.sid, 3),
        )
        self.api.enable_error = 503
        with self.assertRaises(ValueError):
            await self.receipt(first)
        self.api.enable_error = None
        with self.assertRaisesRegex(ValueError, "предыдущего продления"):
            await self.receipt(second)
        self.assertEqual(len(self.api.patches), 1)
        self.assertTrue(await self.receipt(first))
        self.assertTrue(await self.receipt(second))
        self.assertTrue(await self.receipt(first))
        await self.assert_local(datetime(2027, 5, 28, 12, tzinfo=UTC), 3, 2, 0, True)
        self.assertEqual(len(self.api.patches), 2)

    async def test_duplicate_paid_receipt_is_applied_once_under_concurrency(self):
        order = await self.paid_order(self.sid, 1)
        self.assertEqual(
            await asyncio.wait_for(
                asyncio.gather(self.receipt(order), self.receipt(order)), 30
            ),
            [True, True],
        )
        self.assertEqual(len(self.api.patches), 1)
        await self.assert_local(datetime(2027, 2, 28, 12, tzinfo=UTC), 1, 1, 0, True)

    async def test_partial_renewal_cannot_be_abandoned_by_checkout_cancellation(self):
        key = "partial-renewal-cancel"
        self.api.enable_error = 503
        self.assertEqual((await self.checkout(self.sid, 1, key)).status_code, 409)
        cancellation = await self.client.post(
            "/api/web/checkout/cancel",
            json=dict(
                subscription_id=self.sid,
                months=1,
                method="balance",
                idempotency_key=key,
            ),
        )
        self.assertEqual(cancellation.status_code, 409, cancellation.text)
        self.api.enable_error = None
        await self.successful(key)
        self.assertEqual(len(self.api.patches), 1)

    async def test_daily_renewal_recovery_keeps_original_24_hour_target(self):
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            await repo.set_setting(f"auto_renew:{self.sid}", "1")
            await repo.set_setting(f"auto_renew_cycle:{self.sid}", "daily")

        async def daily():
            async with self.db.sessions.begin() as session:
                await prepare_autorenew(SQLRepository(session), self.settings)

        self.api.enable_error = 503
        with self.assertRaises(ValueError):
            await daily()
        await self.assert_local()
        self.api.enable_error = None
        with patch("app.services.billing.now", return_value=NOW + timedelta(hours=3)):
            await daily()
        await daily()
        await self.assert_local(NOW + timedelta(days=1), 1, 1, 500, True)
        self.assertEqual(len(self.api.patches), 1)

    async def test_admin_paid_extension_recovers_without_duplicate_debit(self):
        self.settings.admin_ids = "123"

        async def current(_token):
            return self.user

        self.client._transport.app.include_router(
            admin_dashboard_router(self.db, self.settings, current)
        )
        payload = dict(
            action="extend",
            value="24",
            unit="hours",
            confirmed=True,
            payment_mode="balance",
            amount=500,
            request_id="admin-renewal-fixture",
        )
        self.api.enable_error = 503
        with patch("app.web.admin_dashboard.now", return_value=NOW):
            first = await self.client.post(
                f"/api/web/admin/keys/{self.kid}/action", json=payload
            )
        self.assertEqual(first.status_code, 409, first.text)
        self.api.enable_error = None
        with patch(
            "app.web.admin_dashboard.now", return_value=NOW + timedelta(hours=3)
        ):
            retry = await self.client.post(
                f"/api/web/admin/keys/{self.kid}/action", json=payload
            )
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(
            (
                await self.client.post(
                    f"/api/web/admin/keys/{self.kid}/action", json=payload
                )
            ).status_code,
            200,
        )
        original = (await self.operations())[0].expires_at
        self.assertEqual(original, NOW + timedelta(hours=24))
        await self.assert_local(original, 1, 1, 500, True)
        self.assertEqual(len(self.api.patches), 1)
        async with self.db.sessions() as session:
            payment = await session.scalar(select(Payment))
            sale = json.loads(
                await SQLRepository(session).setting(f"sale_snapshot:{payment.id}")
            )
            self.assertEqual(sale["duration_hours"], 24)

    async def test_bot_manual_extension_uses_persisted_intent_on_retry(self):
        self.settings.admin_ids = "123"
        async with self.db.sessions.begin() as session:
            (await session.get(AccessKey, self.kid)).status = "active"
            await RemnawaveBot(SQLRepository(session), self.settings).render(
                f"admin:r:input:extend:{self.cid}", self.user
            )

        async def extend():
            async with self.db.sessions.begin() as session:
                await RemnawaveBot(SQLRepository(session), self.settings).input(
                    f"admin:r:input:extend:{self.cid}", self.user, "30"
                )

        self.api.enable_error = 503
        with self.assertRaises(ValueError):
            await extend()
        self.api.enable_error = None
        await extend()
        await extend()
        self.assertEqual(len(self.api.patches), 1)
        self.assertEqual(len(self.api.enables), 2)

    async def test_legacy_bot_admin_extension_uses_same_recovery_journal(self):
        self.settings.admin_ids = "123"
        action = f"admin:input:extend:{self.sid}"
        async with self.db.sessions.begin() as session:
            (await session.get(AccessKey, self.kid)).status = "active"
            await AdminService(SQLRepository(session), self.settings).render(
                action, self.user
            )

        async def extend():
            async with self.db.sessions.begin() as session:
                with patch("app.services.admin.now", return_value=NOW):
                    return await AdminService(
                        SQLRepository(session), self.settings
                    ).apply_input(action, "30", self.user)

        self.api.enable_error = 503
        with self.assertRaises(ValueError):
            await extend()
        self.api.enable_error = None
        await extend()
        await extend()
        self.assertEqual(len(self.api.patches), 1)
        self.assertEqual((await self.operations())[0].state, "applied")
        await self.assert_local(NOW + timedelta(days=30), active=True)

    async def test_one_connection_request_pool_does_not_deadlock_journal(self):
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
        await asyncio.wait_for(self.successful("one-pool-connection-renewal"), 20)

    async def test_migration_is_additive_repeatable_and_preserves_keys(self):
        async with self.db.engine.begin() as connection:
            await connection.run_sync(VlessRenewal.__table__.drop)
        await upgrade(self.db.engine)
        await upgrade(self.db.engine)
        await self.assert_local()
        await self.successful("after-renewal-migration")
        await upgrade(self.db.engine)
        self.assertEqual((await self.operations())[0].state, "applied")
        async with self.db.sessions() as session:
            self.assertEqual((await session.get(User, self.uid)).freeman_id, "FO-1")

    async def test_reusing_operation_with_different_terms_fails_before_remote_write(
        self,
    ):
        async with self.db.sessions.begin() as session:
            credential = await session.get(VlessCredential, self.cid)
            await renew(
                SQLRepository(session),
                self.settings,
                credential,
                NOW + timedelta(days=30),
                operation_key="fixed-renewal-intent",
                request=dict(days=30),
                devices=1,
            )
        with self.assertRaisesRegex(ValueError, "Параметры"):
            async with self.db.sessions.begin() as session:
                await renew(
                    SQLRepository(session),
                    self.settings,
                    await session.get(VlessCredential, self.cid),
                    NOW + timedelta(days=90),
                    operation_key="fixed-renewal-intent",
                    request=dict(days=90),
                    devices=3,
                )
        self.assertEqual(len(self.api.patches), 1)

    async def test_missing_operation_identity_fails_before_remote_calls(self):
        with self.assertRaises(ValueError):
            async with self.db.sessions.begin() as session:
                await renew(
                    SQLRepository(session),
                    self.settings,
                    await session.get(VlessCredential, self.cid),
                    NOW + timedelta(days=30),
                    operation_key=None,
                    request=dict(days=30),
                    devices=1,
                )
        self.assertEqual(self.api.lookups, [])
