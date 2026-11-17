"""I05 PostgreSQL renewal quotas; real payments/panel writes are never used."""

import asyncio
import copy
import json
import os
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_billing_concurrency import NOW, BillingConcurrency
from test_vless_renewal import REAL_CONTROL, VlessRenewalConsistency
from test_vless_renewal import Panel as RenewalPanel

from app.database.models import (
    AccessKey,
    Payment,
    Subscription,
    Tariff,
    User,
    VlessCredential,
    VlessRenewal,
)
from app.database.repositories import SQLRepository
from app.services.billing import prepare_autorenew
from app.services.stars import StarService
from app.services.topups import SubscriptionCheckoutService
from app.services.vless_renewal import renew

GIB = 1024**3
BASE = 50 * GIB
TARGET = 100 * GIB
USAGE = 7 * GIB


class Panel(RenewalPanel):
    def __init__(self, remote):
        super().__init__(remote)
        self.ignore_traffic = False

    async def update_user(self, payload):
        before = self.remote["trafficLimitBytes"]
        try:
            return await super().update_user(payload)
        finally:
            if self.ignore_traffic:
                self.remote["trafficLimitBytes"] = before


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class VlessTraffic(unittest.IsolatedAsyncioTestCase):
    checkout = BillingConcurrency.checkout
    receipt = BillingConcurrency.receipt
    operations = VlessRenewalConsistency.operations

    async def asyncSetUp(self):
        await VlessRenewalConsistency.asyncSetUp(self)
        self.api = Panel(
            {
                **self.api.remote,
                "trafficLimitBytes": BASE,
                "trafficLimitStrategy": "NO_RESET",
                "userTraffic": {
                    "usedTrafficBytes": USAGE,
                    "lifetimeUsedTrafficBytes": USAGE,
                },
            }
        )
        self.api.reset_traffic = AsyncMock(
            side_effect=AssertionError("Renewal must not reset traffic")
        )
        async with self.db.sessions.begin() as session:
            credential = await session.get(VlessCredential, self.cid)
            credential.traffic_limit, credential.traffic_used = BASE, USAGE
            await SQLRepository(session).set_setting(
                f"subscription_traffic_limit:{self.sid}", str(BASE)
            )
        await self.tariff(3, TARGET)
        await self.tariff(1, 0)

    async def tariff(self, months, limit, devices=None):
        async with self.db.sessions.begin() as session:
            tariff = await session.scalar(select(Tariff).where(Tariff.months == months))
            if devices is not None:
                tariff.device_limit = devices
            await SQLRepository(session).set_setting(
                f"tariff_traffic:{tariff.id}", str(limit)
            )

    async def paid_order(self, sid, months):
        # Existing financial fixtures predate traffic snapshots. Add the same
        # persisted field created by real YooKassa/Stars/saved-card orders.
        order = await BillingConcurrency.paid_order(self, sid, months)
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            tariff = await repo.tariff(months)
            payload = json.loads(await repo.setting(f"order:{order[0]}"))
            payload["traffic_bytes"] = int(
                await repo.setting(f"tariff_traffic:{tariff.id}", "0")
            )
            await repo.set_setting(f"order:{order[0]}", json.dumps(payload))
        return order

    async def set_base(self, limit):
        async with self.db.sessions.begin() as session:
            (await session.get(VlessCredential, self.cid)).traffic_limit = limit
            await SQLRepository(session).set_setting(
                f"subscription_traffic_limit:{self.sid}", str(limit)
            )
        self.api.remote["trafficLimitBytes"] = limit

    async def ledger(self, count=1, debit=30_000):
        async with self.db.sessions() as session:
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(Payment)), count
            )
            self.assertEqual(
                await SQLRepository(session).balance(self.uid), 100_000 - debit
            )

    async def uncommitted(self):
        async with self.db.sessions() as session:
            credential = await session.get(VlessCredential, self.cid)
            subscription = await session.get(Subscription, self.sid)
            key = await session.get(AccessKey, self.kid)
            self.assertEqual(credential.traffic_limit, BASE)
            self.assertEqual(credential.device_limit, 1)
            self.assertEqual(
                [credential.expires_at, subscription.expires_at, key.expires_at],
                [self.initial] * 3,
            )
            self.assertEqual(
                await SQLRepository(session).setting(
                    f"subscription_traffic_limit:{self.sid}"
                ),
                str(BASE),
            )
            self.assertEqual(credential.traffic_used, USAGE)

    async def duration(self, key, *, days=30, traffic=None, activate=True):
        async with self.db.sessions.begin() as session:
            credential = await session.get(VlessCredential, self.cid)
            subscription = await session.get(Subscription, self.sid)
            return await renew(
                SQLRepository(session),
                self.settings,
                credential,
                max(NOW, subscription.expires_at) + timedelta(days=days),
                operation_key=key,
                request=dict(days=days),
                devices=credential.device_limit,
                traffic_limit=traffic,
                activate=activate,
            )

    async def legacy_journal(self, identity=None):
        async with self.db.sessions.begin() as session:
            operation = (
                await session.get(VlessRenewal, identity)
                if identity
                else await session.scalar(select(VlessRenewal))
            )
            request = json.loads(operation.request)
            request.pop("traffic", None)
            operation.request = json.dumps(request, sort_keys=True)

    async def assert_limits(self, traffic, devices):
        async with self.db.sessions() as session:
            credential = await session.get(VlessCredential, self.cid)
            subscription = await session.get(Subscription, self.sid)
            self.assertEqual(credential.traffic_limit, traffic)
            self.assertEqual(credential.device_limit, devices)
            self.assertEqual(subscription.device_limit, devices)
            self.assertEqual(credential.traffic_used, USAGE)
            self.assertEqual(
                await SQLRepository(session).setting(
                    f"subscription_traffic_limit:{self.sid}"
                ),
                str(traffic),
            )
        self.assertEqual(self.api.remote["trafficLimitBytes"], traffic)
        self.assertEqual(self.api.remote["hwidDeviceLimit"], devices)
        self.assertEqual(self.api.remote["userTraffic"]["usedTrafficBytes"], USAGE)
        self.api.reset_traffic.assert_not_awaited()

    async def test_balance_renewal_applies_purchased_traffic_and_devices(self):
        response = await self.checkout(self.sid, 3, "i05-balance-limited")
        self.assertEqual(response.status_code, 200, response.text)
        await self.assert_limits(TARGET, 3)

    async def test_limited_subscription_can_purchase_unlimited_tariff(self):
        response = await self.checkout(self.sid, 1, "i05-balance-unlimited")
        self.assertEqual(response.status_code, 200, response.text)
        await self.assert_limits(0, 1)

    async def test_paid_order_uses_frozen_quota_after_catalog_changes(self):
        payment_id = await self.paid_order(self.sid, 3)
        await self.tariff(3, 200 * GIB, devices=9)
        await self.receipt(payment_id)
        await self.assert_limits(TARGET, 3)

    async def test_unlimited_renewal_stays_unlimited_without_reset(self):
        await self.set_base(0)
        response = await self.checkout(self.sid, 1, "i05-existing-unlimited")
        self.assertEqual(response.status_code, 200, response.text)
        await self.assert_limits(0, 1)
        await self.ledger(debit=10_000)

    async def test_unlimited_subscription_can_buy_limited_tariff(self):
        await self.set_base(0)
        response = await self.checkout(self.sid, 3, "i05-unlimited-to-limited")
        self.assertEqual(response.status_code, 200, response.text)
        await self.assert_limits(TARGET, 3)

    async def test_no_reset_strategy_or_usage_is_written(self):
        self.api.remote["trafficLimitStrategy"] = "MONTH"
        response = await self.checkout(self.sid, 3, "i05-preserve-reset-policy")
        self.assertEqual(response.status_code, 200, response.text)
        await self.assert_limits(TARGET, 3)
        self.assertEqual(self.api.remote["trafficLimitStrategy"], "MONTH")
        for payload in self.api.patches:
            self.assertTrue(
                set(payload).isdisjoint(
                    {
                        "trafficLimitStrategy",
                        "usedTrafficBytes",
                        "userTraffic",
                        "lastTrafficResetAt",
                    }
                )
            )

    async def test_lost_patch_response_is_verified_without_another_patch(self):
        self.api.lose_patch = True
        response = await self.checkout(self.sid, 3, "i05-lost-patch-response")
        self.assertEqual(response.status_code, 200, response.text)
        await self.assert_limits(TARGET, 3)
        self.assertEqual(len(self.api.patches), 1)
        await self.ledger()

    async def test_partial_enable_failure_recovers_frozen_quota_after_catalog_edit(
        self,
    ):
        key = "i05-enable-recovery-quota"
        self.api.enable_error = 503
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 409)
        await self.uncommitted()
        await self.ledger(count=0, debit=0)
        operation = (await self.operations())[0]
        self.assertEqual(
            json.loads(operation.request)["traffic"],
            dict(base=BASE, limit=TARGET, purchased=True),
        )
        self.assertEqual(self.api.remote["trafficLimitBytes"], TARGET)
        await self.tariff(3, 200 * GIB)
        self.api.enable_error = None
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 200)
        await self.assert_limits(TARGET, 3)
        self.assertEqual(len(self.api.patches), 1)
        await self.ledger()

    async def test_lookup_outage_after_patch_recovers_after_restart(self):
        key = "i05-restart-quota-recovery"
        self.api.lose_patch = self.api.outage_after_write = True
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 409)
        await self.uncommitted()
        self.api = Panel(copy.deepcopy(self.api.remote))
        self.api.reset_traffic = AsyncMock(side_effect=AssertionError("No reset"))
        await self.db.engine.dispose()
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 200)
        self.assertEqual(self.api.patches, [])
        await self.assert_limits(TARGET, 3)
        await self.ledger()

    async def test_confirmed_rejection_keeps_original_quota_and_can_retry(self):
        key = "i05-confirmed-patch-rejection"
        self.api.patch_error = 401
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 409)
        await self.uncommitted()
        await self.ledger(count=0, debit=0)
        self.assertEqual((await self.operations())[0].state, "failed")
        await self.tariff(3, 200 * GIB)
        self.api.patch_error = None
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 200)
        await self.assert_limits(TARGET, 3)
        await self.ledger()

    async def test_partial_traffic_update_cannot_be_charged_or_blindly_repatched(self):
        key = "i05-partial-traffic-update"
        self.api.ignore_traffic = True
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 409)
        await self.uncommitted()
        self.assertEqual((await self.operations())[0].state, "outcome_unknown")
        self.api.ignore_traffic = False
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 409)
        self.assertEqual(len(self.api.patches), 1)
        await self.ledger(count=0, debit=0)
        # Simulate the original uncertain remote PATCH finally becoming visible.
        self.api.remote["trafficLimitBytes"] = TARGET
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 200)
        self.assertEqual(len(self.api.patches), 1)
        await self.assert_limits(TARGET, 3)

    async def test_partial_device_update_cannot_be_reported_as_success(self):
        key = "i05-partial-device-update"
        self.api.ignore_devices = True
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 409)
        await self.uncommitted()
        await self.ledger(count=0, debit=0)
        self.api.remote["hwidDeviceLimit"] = 3
        self.api.ignore_devices = False
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 200)
        self.assertEqual(len(self.api.patches), 1)
        await self.assert_limits(TARGET, 3)

    async def test_external_quota_drift_is_not_silently_overwritten(self):
        self.api.remote["trafficLimitBytes"] = 80 * GIB
        response = await self.checkout(self.sid, 3, "i05-external-quota-drift")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.api.patches, [])
        self.assertEqual(self.api.remote["trafficLimitBytes"], 80 * GIB)
        await self.uncommitted()
        await self.ledger(count=0, debit=0)

    async def test_malformed_or_missing_quota_never_means_unlimited(self):
        key = "i05-malformed-remote-traffic"
        for value in (None, -1, True, "0", 2**63):
            with self.subTest(value=value):
                self.api.remote["trafficLimitBytes"] = value
                self.assertEqual(
                    (await self.checkout(self.sid, 3, key)).status_code, 409
                )
                self.assertEqual(self.api.patches, [])
                await self.uncommitted()
        del self.api.remote["trafficLimitBytes"]
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 409)
        await self.ledger(count=0, debit=0)
        self.api.remote["trafficLimitBytes"] = BASE
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 200)
        await self.assert_limits(TARGET, 3)

    async def test_historical_paid_order_without_quota_preserves_existing_limit(self):
        order = await self.paid_order(self.sid, 3)
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            payload = json.loads(await repo.setting(f"order:{order[0]}"))
            del payload["traffic_bytes"]
            await repo.set_setting(f"order:{order[0]}", json.dumps(payload))
        await self.tariff(3, 200 * GIB)
        await self.receipt(order)
        await self.assert_limits(BASE, 3)
        self.assertNotIn("trafficLimitBytes", self.api.patches[0])

    async def test_legacy_pending_journal_can_recover_same_quota(self):
        key = "i05-legacy-matching-traffic"
        await self.tariff(3, BASE)
        self.api.enable_error = 503
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 409)
        await self.legacy_journal()
        self.api.enable_error = None
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 200)
        await self.assert_limits(BASE, 3)
        self.assertEqual(len(self.api.patches), 1)

    async def test_legacy_pending_journal_with_different_paid_quota_needs_review(self):
        order = await self.paid_order(self.sid, 3)
        self.api.enable_error = 503
        with self.assertRaises(ValueError):
            await self.receipt(order)
        await self.legacy_journal()
        self.api.enable_error = None
        calls = len(self.api.lookups)
        with self.assertRaisesRegex(ValueError, "старом продлении"):
            await self.receipt(order)
        self.assertEqual(len(self.api.lookups), calls)
        await self.uncommitted()
        async with self.db.sessions() as session:
            self.assertEqual(
                (await session.get(Payment, order[0])).status, "order_pending"
            )
            self.assertEqual(
                json.loads(await SQLRepository(session).setting(f"order:{order[0]}"))[
                    "traffic_bytes"
                ],
                TARGET,
            )
        await self.ledger(count=1, debit=0)

    async def test_applied_operation_replay_does_not_restore_old_quota(self):
        await self.duration("i05-first-explicit-quota", traffic=TARGET)
        first = (await self.operations())[0]
        await self.duration("i05-second-unlimited-quota", traffic=0)
        before = len(self.api.patches), len(self.api.lookups)
        await self.legacy_journal(first.id)
        result = await self.duration("i05-first-explicit-quota", traffic=TARGET)
        self.assertTrue(result.replayed)
        self.assertEqual((len(self.api.patches), len(self.api.lookups)), before)
        await self.assert_limits(0, 1)

    async def test_duration_only_extension_preserves_quota_without_quota_patch(self):
        await self.duration("i05-admin-duration-only", activate=False)
        await self.assert_limits(BASE, 1)
        self.assertNotIn("trafficLimitBytes", self.api.patches[0])
        self.assertEqual(self.api.remote["status"], "DISABLED")
        await self.ledger(count=0, debit=0)

    async def test_daily_extension_preserves_quota_and_usage(self):
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            for name, value in (
                ("auto_renew", "1"),
                ("auto_renew_cycle", "daily"),
                ("auto_source", "balance"),
            ):
                await repo.set_setting(f"{name}:{self.sid}", value)
        async with self.db.sessions.begin() as session:
            await prepare_autorenew(SQLRepository(session), self.settings)
        await self.assert_limits(BASE, 1)
        self.assertNotIn("trafficLimitBytes", self.api.patches[0])
        await self.ledger(debit=500)

    async def test_failed_duration_extension_does_not_restore_quota_after_new_purchase(
        self,
    ):
        self.api.patch_error = 401
        with self.assertRaises(ValueError):
            await self.duration("i05-duration-rebase")
        self.api.patch_error = None
        self.assertEqual(
            (
                await self.checkout(self.sid, 1, "i05-new-unlimited-purchase")
            ).status_code,
            200,
        )
        await self.duration("i05-duration-rebase")
        await self.assert_limits(0, 1)
        self.assertNotIn("trafficLimitBytes", self.api.patches[-1])

    async def concurrent_quotas(self, first_months, second_months, duplicate=False):
        self.api.entered, self.api.release = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(
            self.checkout(self.sid, first_months, "i05-concurrent-first")
        )
        try:
            await asyncio.wait_for(self.api.entered.wait(), 10)
            second = asyncio.create_task(
                self.checkout(
                    self.sid,
                    second_months,
                    "i05-concurrent-first" if duplicate else "i05-concurrent-second",
                )
            )
            await asyncio.sleep(0.1)
            self.assertFalse(second.done())
        finally:
            self.api.release.set()
        responses = await asyncio.wait_for(asyncio.gather(first, second), 20)
        self.assertEqual(
            [r.status_code for r in responses], [200, 200], [r.text for r in responses]
        )
        await self.assert_limits(TARGET if second_months == 3 else 0, second_months)
        await self.ledger(
            count=1 if duplicate else 2, debit=30_000 if duplicate else 40_000
        )
        self.assertEqual(len(self.api.patches), 1 if duplicate else 2)

    async def test_concurrent_limited_then_unlimited_renewals(self):
        await self.concurrent_quotas(3, 1)

    async def test_concurrent_unlimited_then_limited_renewals(self):
        await self.concurrent_quotas(1, 3)

    async def test_duplicate_concurrent_purchase_updates_quota_and_charges_once(self):
        await self.concurrent_quotas(3, 3, duplicate=True)

    async def test_local_rollback_preserves_durable_remote_quota_for_retry(self):
        key = "i05-local-rollback-quota"

        async def control(*args, **kwargs):
            await REAL_CONTROL(*args, **kwargs)
            raise ValueError("Injected caller rollback after confirmed renewal")

        with patch("app.services.vless_provision.control", side_effect=control):
            self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 409)
        await self.uncommitted()
        await self.ledger(count=0, debit=0)
        self.assertEqual((await self.operations())[0].state, "remote_confirmed")
        self.assertEqual(self.api.remote["trafficLimitBytes"], TARGET)
        self.assertEqual((await self.checkout(self.sid, 3, key)).status_code, 200)
        await self.assert_limits(TARGET, 3)
        self.assertEqual(len(self.api.patches), 1)
        await self.ledger()

    async def test_journal_commit_failure_recovers_quota_without_repatch(self):
        armed = True

        def fail(session):
            nonlocal armed
            if armed and any(
                isinstance(row, VlessRenewal) and row.state == "patch_confirmed"
                for row in session.dirty
            ):
                armed = False
                raise SQLAlchemyError("Injected traffic confirmation commit failure")

        event.listen(Session, "before_commit", fail)
        try:
            self.assertEqual(
                (
                    await self.checkout(self.sid, 3, "i05-journal-commit-failure")
                ).status_code,
                409,
            )
        finally:
            event.remove(Session, "before_commit", fail)
        self.assertFalse(armed)
        await self.uncommitted()
        self.assertEqual(
            (
                await self.checkout(self.sid, 3, "i05-journal-commit-failure")
            ).status_code,
            200,
        )
        await self.assert_limits(TARGET, 3)
        self.assertEqual(len(self.api.patches), 1)
        await self.ledger()

    async def test_paid_unknown_response_keeps_original_order_and_recovers_once(self):
        order = await self.paid_order(self.sid, 3)
        self.api.lose_patch = self.api.outage_after_write = True
        with self.assertRaises(ValueError):
            await self.receipt(order)
        await self.uncommitted()
        async with self.db.sessions() as session:
            self.assertEqual(
                (await session.get(Payment, order[0])).status, "order_pending"
            )
        self.api.outage = self.api.lose_patch = False
        self.assertTrue(await self.receipt(order))
        self.assertTrue(await self.receipt(order))
        await self.assert_limits(TARGET, 3)
        self.assertEqual(len(self.api.patches), 1)
        await self.ledger(count=1, debit=0)

    async def test_real_yookassa_order_records_quota_before_payment(self):
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            payment = await SubscriptionCheckoutService(
                repo, self.settings
            ).create_order(self.user, 3, self.sid)
            terms = json.loads(await repo.setting(f"order:{payment.id}"))
            self.assertEqual((terms["traffic_bytes"], terms["devices"]), (TARGET, 3))
        self.assertEqual(self.api.patches, [])

    async def test_stars_renewal_uses_saved_quota_after_tariff_changes(self):
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            await repo.set_setting("payment_provider:stars", "1")
            await repo.set_setting("stars_price:3", "300")
            invoice = await StarService(repo, self.settings).create(
                self.user, 3, self.sid
            )
        await self.tariff(3, 200 * GIB, devices=9)
        successful = SimpleNamespace(
            invoice_payload=invoice["payload"],
            currency="XTR",
            total_amount=300,
            telegram_payment_charge_id="fixture-i05-stars",
        )
        with patch.object(
            StarService, "success_screen", new=AsyncMock(return_value=None)
        ):
            async with self.db.sessions.begin() as session:
                await StarService(SQLRepository(session), self.settings).complete(
                    successful, await session.get(User, self.uid)
                )
        await self.assert_limits(TARGET, 3)
        await self.ledger(count=1, debit=0)

    async def test_invalid_paid_quota_is_rejected_without_activation(self):
        order = await self.paid_order(self.sid, 3)
        for value in (None, True, -1, "0", 2**63):
            with self.subTest(value=value):
                async with self.db.sessions.begin() as session:
                    repo = SQLRepository(session)
                    terms = json.loads(await repo.setting(f"order:{order[0]}"))
                    terms["traffic_bytes"] = value
                    await repo.set_setting(f"order:{order[0]}", json.dumps(terms))
                with self.assertRaises(ValueError):
                    await self.receipt(order)
                self.assertEqual(self.api.patches, [])
                await self.uncommitted()
        await self.ledger(count=1, debit=0)
