"""PostgreSQL renewal regressions; no real payments, VPN calls or bot sends."""

import asyncio
import json
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI
from sqlalchemy import event, select
from test_freeman_ai import Tests as DatabaseFixture

from app.config.settings import Settings
from app.database.models import (
    AccessKey,
    BalanceTransaction,
    Payment,
    RemnawaveServer,
    Subscription,
    Tariff,
    User,
    VlessCredential,
)
from app.database.repositories import SQLRepository
from app.services import vless_provision
from app.services.billing import BillingService, prepare_autorenew
from app.services.topups import SubscriptionCheckoutService
from app.web.cabinet.payments import payments_router

UTC = timezone.utc
NOW = datetime(2027, 1, 31, 12, tzinfo=UTC)
INITIAL_BALANCE = 100_000


@unittest.skipUnless(
    os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL row locks"
)
class BillingConcurrency(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = urlsplit(os.environ.get("DATABASE_URL", ""))
        name = url.path.lstrip("/")
        if url.scheme not in {"postgresql", "postgresql+asyncpg"} or not (
            name.startswith("test_") or name.endswith(("_test", "_tests"))
        ):
            raise RuntimeError("Use a disposable test PostgreSQL database")
        await DatabaseFixture.asyncSetUp(self)
        self.addAsyncCleanup(DatabaseFixture.asyncTearDown, self)
        # These existing financial fixtures deliberately use fake/demo keys.
        self.db.sessions.configure(info={"isolated_demo_provisioning": True})
        self.settings = Settings(
            _env_file=None,
            bot_token="",
            encryption_key=self.settings.encryption_key,
            yookassa_shop_id="fixture-shop",
            yookassa_secret_key="fixture-not-a-real-secret",
            web_base_url="https://example.test",
        )
        async with self.db.sessions.begin() as session:
            self.user = await session.get(User, self.uid)
            session.add_all(
                [Tariff(months=m, price=m * 10_000, device_limit=m) for m in (1, 3)]
            )
            session.add(
                BalanceTransaction(
                    user_id=self.uid,
                    type="fixture",
                    amount=INITIAL_BALANCE,
                    description="Test funding",
                )
            )

        async def current_user(_token):
            return self.user

        app = FastAPI()
        app.include_router(
            payments_router(
                SimpleNamespace(
                    database=self.db,
                    settings=self.settings,
                    current_user=current_user,
                    cipher=None,
                )
            )
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=self.settings.web_base_url
        )
        self.addAsyncCleanup(self.client.aclose)
        self.controls = []

        async def control(
            _repo, _settings, credential, operation, expiry=None, **_kwargs
        ):
            renewal = _kwargs.get("renewal")
            devices = renewal["devices"] if renewal else credential.device_limit
            self.controls.append((operation, expiry, devices))
            # Renewal now coordinates enable internally; preserve this fixture's
            # original simulated remote steps and all C01 assertions.
            if renewal and operation == "extend" and credential.status != "active":
                self.controls.append(("unblock", None, devices))

        for item in (
            patch("app.services.billing.now", return_value=NOW),
            patch("app.services.vless_provision.control", side_effect=control),
        ):
            item.start()
            self.addCleanup(item.stop)

    async def subscription(self, expired=False):
        expiry = (
            datetime(2026, 12, 31, 12, tzinfo=UTC)
            if expired
            else datetime(2028, 1, 31, 12, tzinfo=UTC)
        )
        async with self.db.sessions.begin() as session:
            sub = Subscription(user_id=self.uid, kind="paid", expires_at=expiry)
            server = RemnawaveServer(
                name="Fixture",
                country="TEST",
                api_url="https://vpn.invalid",
                api_token="fixture",
                internal_squad_uuid="fixture-squad",
            )
            session.add_all([sub, server])
            await session.flush()
            key = AccessKey(
                user_id=self.uid,
                name="Existing key",
                mode="vless",
                expires_at=expiry,
                status="billing_blocked" if expired else "active",
            )
            session.add(key)
            await session.flush()
            session.add(
                VlessCredential(
                    server_id=server.id,
                    user_id=self.uid,
                    subscription_id=sub.id,
                    access_key_id=key.id,
                    remote_user_uuid="fixture-remote",
                    subscription_url="https://sub.example.test/fixture",
                    expires_at=expiry,
                    device_limit=1,
                    status="disabled" if expired else "active",
                )
            )
            repo = SQLRepository(session)
            await repo.set_setting(f"subscription_key:{sub.id}", str(key.id))
            if expired:
                await repo.set_setting(f"billing_blocked:{key.id}", "1")
            return sub.id, key.id, expiry

    async def checkout(self, sid, months, request_id, method="balance"):
        return await self.client.post(
            "/api/web/checkout",
            json=dict(
                subscription_id=sid,
                months=months,
                method=method,
                idempotency_key=request_id,
            ),
        )

    async def ordered_pair(self, sid, first, second):
        """Both transactions preload old state; second waits on first's DB lock."""
        original = BillingService.valid
        loaded = [asyncio.Event(), asyncio.Event()]
        proceed = [asyncio.Event(), asyncio.Event()]
        provisioning = asyncio.Event()
        release_provider = asyncio.Event()
        contender = asyncio.Event()
        tasks = []

        async def valid(service, user, subscription_id, **kwargs):
            result = await original(service, user, subscription_id, **kwargs)
            if not kwargs.get("for_update") and subscription_id == sid:
                index = tasks.index(asyncio.current_task())
                loaded[index].set()
                await asyncio.wait_for(proceed[index].wait(), 5)
            return result

        original_control = vless_provision.control

        async def control(*args, **kwargs):
            if asyncio.current_task() is tasks[0] and args[3] == "extend":
                provisioning.set()
                await asyncio.wait_for(release_provider.wait(), 5)
            return await original_control(*args, **kwargs)

        def executing(_conn, _cursor, statement, _params, _context, _many):
            if len(tasks) == 2 and asyncio.current_task() is tasks[1]:
                if statement.startswith("UPDATE users ") or (
                    "FROM subscriptions" in statement and "FOR UPDATE" in statement
                ):
                    contender.set()

        event.listen(self.db.engine.sync_engine, "before_cursor_execute", executing)
        try:
            with (
                patch.object(BillingService, "valid", valid),
                patch("app.services.vless_provision.control", side_effect=control),
            ):
                tasks.extend(
                    [asyncio.create_task(first()), asyncio.create_task(second())]
                )
                await asyncio.wait_for(asyncio.gather(*(e.wait() for e in loaded)), 5)
                proceed[0].set()
                await asyncio.wait_for(provisioning.wait(), 5)
                proceed[1].set()
                await asyncio.wait_for(contender.wait(), 5)
                self.assertFalse(tasks[1].done())
                release_provider.set()
                return await asyncio.wait_for(asyncio.gather(*tasks), 15)
        finally:
            event.remove(self.db.engine.sync_engine, "before_cursor_execute", executing)
            release_provider.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def assert_state(self, sid, kid, expiry, months, payment_count=2, debit=None):
        async with self.db.sessions() as session:
            sub = await session.get(Subscription, sid)
            key = await session.get(AccessKey, kid)
            credential = await session.scalar(
                select(VlessCredential).where(VlessCredential.subscription_id == sid)
            )
            payments = list(
                await session.scalars(
                    select(Payment).where(Payment.subscription_id == sid)
                )
            )
            self.assertEqual(len(payments), payment_count)
            self.assertEqual(sub.expires_at, expiry)
            self.assertEqual(key.expires_at, expiry)
            self.assertEqual(credential.expires_at, expiry)
            self.assertEqual(key.status, "active")
            self.assertEqual(credential.status, "active")
            self.assertEqual(sub.device_limit, months)
            self.assertEqual(credential.device_limit, months)
            self.assertEqual(
                await SQLRepository(session).balance(self.uid),
                INITIAL_BALANCE
                - (sum(p.amount for p in payments) if debit is None else debit),
            )
            return payments

    async def balance_pair(self, months, expired=False):
        sid, kid, _ = await self.subscription(expired)
        # Calendar-month clamping deliberately makes these two orders distinct.
        expected = (
            datetime(2027, 5, 28 if months[0] == 1 else 30, 12, tzinfo=UTC)
            if expired
            else datetime(2028, 5, 29 if months[0] == 1 else 30, 12, tzinfo=UTC)
        )
        if months == (1, 1):
            expected = (
                datetime(2027, 3, 28, 12, tzinfo=UTC)
                if expired
                else datetime(2028, 3, 29, 12, tzinfo=UTC)
            )
        results = await self.ordered_pair(
            sid,
            lambda: self.checkout(sid, months[0], "independent-first"),
            lambda: self.checkout(sid, months[1], "independent-second"),
        )
        self.assertEqual([r.status_code for r in results], [200, 200])
        self.assertEqual([r.json()["key_id"] for r in results], [kid, kid])
        await self.assert_state(sid, kid, expected, months[-1])
        self.assertEqual(
            [x[1] for x in self.controls if x[0] == "extend"][-1], expected
        )

    async def test_balance_renewals_one_then_three_months(self):
        await self.balance_pair((1, 3))

    async def test_balance_renewals_three_then_one_month(self):
        await self.balance_pair((3, 1))

    async def test_expired_renewals_one_then_three_months(self):
        await self.balance_pair((1, 3), expired=True)

    async def test_expired_renewals_three_then_one_month(self):
        await self.balance_pair((3, 1), expired=True)

    async def test_two_independent_one_month_renewals(self):
        await self.balance_pair((1, 1))

    async def test_subscription_row_lock_waits_and_refreshes_preloaded_state(self):
        sid, kid, _ = await self.subscription()
        loaded, writer_ready, proceed, release, locked = [
            asyncio.Event() for _ in range(5)
        ]
        tasks = []

        async def reader():
            async with self.db.sessions.begin() as session:
                service = BillingService(SQLRepository(session), self.settings)
                sub, key = await service.valid(self.user, sid)
                loaded.set()
                await proceed.wait()
                await service.fulfill(self.user, await service.repo.tariff(1), sub, key)

        async def writer():
            async with self.db.sessions.begin() as session:
                sub = await session.scalar(
                    select(Subscription).where(Subscription.id == sid).with_for_update()
                )
                sub.expires_at = datetime(2030, 6, 30, 12, tzinfo=UTC)
                await session.flush()
                writer_ready.set()
                await release.wait()

        def executing(_conn, _cursor, statement, _params, _context, _many):
            if (
                tasks
                and asyncio.current_task() is tasks[0]
                and "FROM subscriptions" in statement
                and "FOR UPDATE" in statement
            ):
                locked.set()

        event.listen(self.db.engine.sync_engine, "before_cursor_execute", executing)
        try:
            tasks.append(asyncio.create_task(reader()))
            await asyncio.wait_for(loaded.wait(), 5)
            tasks.append(asyncio.create_task(writer()))
            await asyncio.wait_for(writer_ready.wait(), 5)
            proceed.set()
            await asyncio.wait_for(locked.wait(), 5)
            self.assertEqual(self.controls, [])
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 15)
        finally:
            event.remove(self.db.engine.sync_engine, "before_cursor_execute", executing)
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.assert_state(
            sid, kid, datetime(2030, 7, 30, 12, tzinfo=UTC), 1, 0, debit=0
        )

    async def test_bot_balance_callbacks_renew_each_operation_once(self):
        sid, kid, _ = await self.subscription()
        async with self.db.sessions.begin() as session:
            service = BillingService(SQLRepository(session), self.settings)
            first = await service.preview(self.user, 1, sid)
            second = await service.preview(self.user, 1, sid)

        async def callback(action):
            async with self.db.sessions.begin() as session:
                return await BillingService(
                    SQLRepository(session), self.settings
                ).render(action, self.user)

        await asyncio.wait_for(
            asyncio.gather(*(callback(a) for a in (first, second, first, second))), 15
        )
        await self.assert_state(sid, kid, datetime(2028, 3, 29, 12, tzinfo=UTC), 1)
        self.assertEqual(len(self.controls), 2)

    async def duplicate_checkout(self, method):
        sid, kid, original = await self.subscription()
        ready, count = asyncio.Event(), 0
        original_setting = SQLRepository.setting

        async def setting(repo, name, default=""):
            nonlocal count
            value = await original_setting(repo, name, default)
            if name == "tariff_enabled:1":
                count += 1
                if count == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 5)
            return value

        async def provider_create(payment, *_args, **_kwargs):
            return dict(
                id=f"fixture-{payment.id}",
                confirmation=dict(
                    confirmation_url=f"https://payments.example.test/{payment.id}"
                ),
            )

        with (
            patch.object(SQLRepository, "setting", setting),
            patch(
                "app.services.topups.YooKassa.create", side_effect=provider_create
            ) as create,
        ):
            responses = await asyncio.wait_for(
                asyncio.gather(
                    *[self.checkout(sid, 1, "same-operation", method) for _ in range(2)]
                ),
                15,
            )
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(responses[0].json(), responses[1].json())
        self.assertEqual(create.call_count, 1 if method == "yookassa" else 0)
        if method == "balance":
            await self.assert_state(
                sid, kid, datetime(2028, 2, 29, 12, tzinfo=UTC), 1, 1
            )
            self.assertEqual(len(self.controls), 1)
        else:
            async with self.db.sessions() as session:
                self.assertEqual(len(list(await session.scalars(select(Payment)))), 1)
                self.assertEqual(
                    (await session.get(Subscription, sid)).expires_at, original
                )

    async def test_duplicate_balance_checkout_returns_original_result(self):
        await self.duplicate_checkout("balance")

    async def test_duplicate_card_checkout_creates_one_invoice(self):
        await self.duplicate_checkout("yookassa")

    async def test_duplicate_shared_charge_reference_is_not_applied_twice(self):
        sid, kid, _ = await self.subscription()

        async def charge():
            async with self.db.sessions.begin() as session:
                repo = SQLRepository(session)
                service = BillingService(repo, self.settings)
                sub, key = await service.valid(self.user, sid)
                return (
                    await service.charge(
                        self.user,
                        await repo.tariff(1),
                        sub,
                        key,
                        10_000,
                        "fixture-charge",
                    )
                ).id

        self.assertEqual(await self.ordered_pair(sid, charge, charge), [sid, sid])
        await self.assert_state(sid, kid, datetime(2028, 2, 29, 12, tzinfo=UTC), 1, 1)
        self.assertEqual(len(self.controls), 1)

    async def paid_order(self, sid, months):
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            payment = await repo.add(
                Payment(
                    user_id=self.uid,
                    amount=10_000 * months,
                    status="order_pending",
                    provider_id=f"fixture-card-{months}",
                )
            )
            await repo.set_setting(
                f"order:{payment.id}",
                json.dumps(
                    dict(
                        months=months,
                        sid=sid,
                        devices=months,
                        amount=payment.amount,
                        total=payment.amount,
                        balance_applied=0,
                    )
                ),
            )
            return payment.id, dict(
                id=payment.provider_id,
                status="succeeded",
                paid=True,
                test=True,
                amount=dict(value=f"{payment.amount / 100:.2f}", currency="RUB"),
                metadata=dict(
                    freeman_payment=str(payment.id),
                    freeman_user=str(self.uid),
                    freeman_kind="subscription",
                    freeman_months=str(months),
                    freeman_sid=str(sid),
                ),
            )

    async def receipt(self, order):
        pid, data = order
        async with self.db.sessions.begin() as session:
            payment = await session.get(Payment, pid)
            return await SubscriptionCheckoutService(
                SQLRepository(session), self.settings
            ).credit(payment, data)

    async def test_two_card_receipts_and_duplicate_delivery(self):
        sid, kid, _ = await self.subscription()
        first, second = await self.paid_order(sid, 1), await self.paid_order(sid, 3)
        self.assertEqual(
            await self.ordered_pair(
                sid, lambda: self.receipt(first), lambda: self.receipt(second)
            ),
            [True, True],
        )
        self.assertEqual(
            await asyncio.gather(self.receipt(first), self.receipt(second)),
            [True, True],
        )
        await self.assert_state(
            sid, kid, datetime(2028, 5, 29, 12, tzinfo=UTC), 3, debit=0
        )
        self.assertEqual(len(self.controls), 2)

    async def test_balance_and_card_receipt_share_fresh_subscription_state(self):
        sid, kid, _ = await self.subscription()
        order = await self.paid_order(sid, 3)
        result, receipt = await self.ordered_pair(
            sid,
            lambda: self.checkout(sid, 1, "wallet-and-card"),
            lambda: self.receipt(order),
        )
        self.assertEqual(result.status_code, 200)
        self.assertTrue(receipt)
        await self.assert_state(
            sid, kid, datetime(2028, 5, 29, 12, tzinfo=UTC), 3, debit=10_000
        )

    async def test_admin_checkout_keeps_free_renewal_and_replay_semantics(self):
        self.settings.admin_ids = "123"
        sid, kid, _ = await self.subscription()
        responses = await self.ordered_pair(
            sid,
            lambda: self.checkout(sid, 1, "owner-first-operation"),
            lambda: self.checkout(sid, 3, "owner-second-operation"),
        )
        self.assertEqual([r.status_code for r in responses], [200, 200])
        replay = await self.checkout(sid, 3, "owner-second-operation")
        self.assertEqual(replay.json(), responses[1].json())
        await self.assert_state(
            sid, kid, datetime(2028, 5, 29, 12, tzinfo=UTC), 3, 0, debit=0
        )

    async def test_daily_autorenew_waits_for_paid_renewal_before_due_check(self):
        sid, kid, _ = await self.subscription(expired=True)
        order = await self.paid_order(sid, 1)
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            await repo.set_setting(f"auto_renew:{sid}", "1")
            await repo.set_setting(f"auto_renew_cycle:{sid}", "daily")
        provisioning, release, locked = [asyncio.Event() for _ in range(3)]
        tasks = []
        original_control = vless_provision.control

        async def control(*args, **kwargs):
            if asyncio.current_task() is tasks[0] and args[3] == "extend":
                provisioning.set()
                await asyncio.wait_for(release.wait(), 5)
            return await original_control(*args, **kwargs)

        async def daily():
            async with self.db.sessions.begin() as session:
                await prepare_autorenew(SQLRepository(session), self.settings)

        def executing(_conn, _cursor, statement, _params, _context, _many):
            if (
                len(tasks) == 2
                and asyncio.current_task() is tasks[1]
                and "FROM subscriptions" in statement
                and "FOR UPDATE" in statement
            ):
                locked.set()

        event.listen(self.db.engine.sync_engine, "before_cursor_execute", executing)
        try:
            with patch("app.services.vless_provision.control", side_effect=control):
                tasks.append(asyncio.create_task(self.receipt(order)))
                await asyncio.wait_for(provisioning.wait(), 5)
                tasks.append(asyncio.create_task(daily()))
                await asyncio.wait_for(locked.wait(), 5)
                self.assertFalse(tasks[1].done())
                release.set()
                await asyncio.wait_for(asyncio.gather(*tasks), 15)
        finally:
            event.remove(self.db.engine.sync_engine, "before_cursor_execute", executing)
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.assert_state(
            sid, kid, datetime(2027, 2, 28, 12, tzinfo=UTC), 1, 1, debit=0
        )
        self.assertEqual([op for op, _, _ in self.controls], ["extend", "unblock"])

    async def test_failed_provisioning_rolls_back_and_same_request_can_retry(self):
        sid, kid, original = await self.subscription()
        with patch(
            "app.services.vless_provision.control",
            side_effect=ValueError("Provider unavailable"),
        ):
            failed = await self.checkout(sid, 1, "retry-after-rollback")
        self.assertEqual(failed.status_code, 409)
        await self.assert_state(sid, kid, original, 1, 0, debit=0)
        self.assertEqual(
            (await self.checkout(sid, 1, "retry-after-rollback")).status_code, 200
        )
        await self.assert_state(sid, kid, datetime(2028, 2, 29, 12, tzinfo=UTC), 1, 1)
