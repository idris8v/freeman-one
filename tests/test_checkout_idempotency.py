"""Real PostgreSQL checkout contracts; providers are always isolated fakes."""

import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import func, select
from test_billing_concurrency import BillingConcurrency

from app.database.models import (
    AccessKey,
    BalanceTransaction,
    Payment,
    Subscription,
    User,
)
from app.database.repositories import SQLRepository
from app.services.topups import SubscriptionCheckoutService


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class CheckoutIdempotency(unittest.IsolatedAsyncioTestCase):
    subscription = BillingConcurrency.subscription
    checkout = BillingConcurrency.checkout
    receipt = BillingConcurrency.receipt
    paid_order = BillingConcurrency.paid_order

    async def asyncSetUp(self):
        await BillingConcurrency.asyncSetUp(self)
        self.provisioned = []
        self.invoices = {}
        self.attempts = []

        async def ensure(repo, _settings, sub, user, **_kwargs):
            self.provisioned.append(sub.id)
            key = await repo.add(
                AccessKey(
                    user_id=user.id,
                    name="Fixture key",
                    mode="mock",
                    expires_at=sub.expires_at,
                )
            )
            await repo.set_setting(f"subscription_key:{sub.id}", str(key.id))
            return SimpleNamespace(access_key_id=key.id)

        async def create(payment, key, *args, **kwargs):
            self.attempts.append((payment.id, key))
            return self.invoices.setdefault(
                key,
                {
                    "id": f"fixture-{payment.id}",
                    "confirmation": {
                        "confirmation_url": f"https://payments.example.test/{payment.id}"
                    },
                },
            )

        self.create = create
        for item in (
            patch("app.services.vless_provision.ensure", side_effect=ensure),
            patch("app.services.topups.YooKassa.create", side_effect=create),
        ):
            item.start()
            self.addCleanup(item.stop)

    async def counts(self):
        async with self.db.sessions() as session:
            return tuple(
                [
                    await session.scalar(select(func.count()).select_from(model))
                    for model in (Payment, Subscription, AccessKey)
                ]
            )

    async def status(self, key):
        return await self.client.get(
            "/api/web/checkout", params={"idempotency_key": key}
        )

    async def parallel(self, method):
        key = f"parallel-new-{method}"
        marker = f"web_checkout:{self.uid}:{key}"
        original = SQLRepository.setting
        ready, count = asyncio.Event(), 0

        async def setting(repo, name, default=""):
            nonlocal count
            value = await original(repo, name, default)
            if name == marker and not value:
                count += 1
                if count == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 5)
            return value

        with patch.object(SQLRepository, "setting", setting):
            replies = await asyncio.wait_for(
                asyncio.gather(
                    *[self.checkout(None, 1, key, method) for _ in range(2)]
                ),
                15,
            )
        self.assertEqual([r.status_code for r in replies], [200, 200])
        self.assertEqual(replies[0].json(), replies[1].json())
        self.assertEqual(
            await self.counts(), (1, 1, 1) if method == "balance" else (1, 0, 0)
        )
        self.assertEqual(len(self.provisioned), 1 if method == "balance" else 0)
        self.assertEqual(len(self.invoices), 0 if method == "balance" else 1)
        self.assertEqual(len(self.attempts), 0 if method == "balance" else 1)
        return replies[0].json()

    async def test_concurrent_new_balance_purchase_and_lost_response(self):
        original = await self.parallel("balance")
        retry = await self.checkout(None, 1, "parallel-new-balance")
        self.assertEqual(retry.json(), original)
        self.assertEqual(await self.counts(), (1, 1, 1))
        async with self.db.sessions() as session:
            self.assertEqual(await SQLRepository(session).balance(self.uid), 90_000)

    async def test_concurrent_new_card_purchase_and_lost_response(self):
        original = await self.parallel("yookassa")
        self.assertEqual(
            (await self.checkout(None, 1, "parallel-new-yookassa", "yookassa")).json(),
            original,
        )
        self.assertEqual(
            (await self.status("parallel-new-yookassa")).json()["result"], original
        )
        self.assertEqual(len(self.attempts), 1)

    async def conflicts(self, method):
        sid, _, _ = await self.subscription()
        key = f"parameter-contract-{method}"
        body = dict(months=1, method=method, subscription_id=sid, idempotency_key=key)
        first = await self.client.post("/api/web/checkout", json=body)
        self.assertEqual(first.status_code, 200)
        for changed in (
            {"months": 3},
            {"months": 777},
            {"method": "balance" if method == "yookassa" else "yookassa"},
            {"use_balance": True},
            {"subscription_id": None},
        ):
            with self.subTest(changed=changed):
                response = await self.client.post(
                    "/api/web/checkout", json={**body, **changed}
                )
                self.assertEqual(response.status_code, 409, response.text)
        # Explicit defaults and omitted defaults are the same request.
        replay = await self.client.post(
            "/api/web/checkout", json={**body, "use_balance": False}
        )
        self.assertEqual(replay.json(), first.json())
        self.assertEqual((await self.counts())[0], 1)

    async def test_balance_request_fingerprint_checks_every_parameter(self):
        await self.conflicts("balance")

    async def test_card_request_fingerprint_checks_every_parameter(self):
        await self.conflicts("yookassa")

    async def test_concurrent_conflicting_parameters_do_not_create_second_order(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def create(*args, **kwargs):
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return await self.create(*args, **kwargs)

        with patch("app.services.topups.YooKassa.create", side_effect=create):
            task = asyncio.create_task(
                self.checkout(None, 1, "concurrent-conflict", "yookassa")
            )
            try:
                await asyncio.wait_for(entered.wait(), 5)
                conflict = await asyncio.wait_for(
                    self.checkout(None, 3, "concurrent-conflict", "yookassa"), 5
                )
                self.assertEqual(conflict.status_code, 409)
            finally:
                release.set()
            self.assertEqual((await task).status_code, 200)
        self.assertEqual(await self.counts(), (1, 0, 0))
        self.assertEqual(len(self.invoices), 1)

    async def test_provider_response_loss_reuses_committed_order_and_provider_key(self):
        key = "provider-response-lost"
        async with self.db.sessions.begin() as session:
            # Partial balance payment also reserves money exactly once.
            session.add(
                BalanceTransaction(
                    user_id=self.uid,
                    type="fixture",
                    amount=-95_000,
                    description="Fixture",
                )
            )
        body = dict(months=1, method="yookassa", use_balance=True, idempotency_key=key)
        lost = True

        async def create(*args, **kwargs):
            nonlocal lost
            invoice = await self.create(*args, **kwargs)
            if lost:
                lost = False
                raise ValueError("Injected timeout after provider created invoice")
            return invoice

        with patch("app.services.topups.YooKassa.create", side_effect=create):
            first = await self.client.post("/api/web/checkout", json=body)
            self.assertEqual(first.status_code, 409)
            operation = (await self.status(key)).json()
            self.assertIsNone(operation["result"])
            self.assertTrue(operation["payment_id"])
            conflict = await self.client.post(
                "/api/web/checkout", json={**body, "use_balance": False}
            )
            self.assertEqual(conflict.status_code, 409)
            second = await self.client.post("/api/web/checkout", json=body)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["payment_id"], operation["payment_id"])
        self.assertEqual(self.attempts[0], self.attempts[1])
        self.assertEqual(len(self.invoices), 1)
        self.assertEqual(await self.counts(), (1, 0, 0))
        self.assertEqual(
            (await self.client.post("/api/web/checkout", json=body)).json(),
            second.json(),
        )
        async with self.db.sessions() as session:
            self.assertEqual(await SQLRepository(session).balance(self.uid), 0)
            self.assertEqual(
                await session.scalar(
                    select(func.count())
                    .select_from(BalanceTransaction)
                    .where(BalanceTransaction.type == "checkout_hold")
                ),
                1,
            )

    async def test_pending_result_replays_after_tariff_is_disabled(self):
        key = "pending-tariff-changed"
        first = await self.checkout(None, 1, key, "yookassa")
        async with self.db.sessions.begin() as session:
            await SQLRepository(session).set_setting("tariff_enabled:1", "0")
        replay = await self.checkout(None, 1, key, "yookassa")
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(len(self.attempts), 1)

    async def test_concurrent_replay_wins_over_later_catalog_validation_error(self):
        original = SQLRepository.setting
        for method in ("balance", "yookassa"):
            with self.subTest(method=method):
                both, committed = asyncio.Event(), asyncio.Event()
                seen = set()
                leader = None

                async def setting(repo, name, default=""):
                    task = asyncio.current_task()
                    # Coordinate the two HTTP executors, not every catalogue
                    # read (invoice staging also validates the same tariff).
                    if name == "tariff_enabled:1" and task not in seen:
                        seen.add(task)
                        if task is leader:
                            await asyncio.wait_for(both.wait(), 5)
                        else:
                            both.set()
                            await asyncio.wait_for(committed.wait(), 5)
                            return "0"
                    return await original(repo, name, default)

                async def first():
                    nonlocal leader
                    leader = asyncio.current_task()
                    response = await self.checkout(
                        None, 1, "catalog-race-" + method, method
                    )
                    committed.set()
                    return response

                with patch.object(SQLRepository, "setting", setting):
                    responses = await asyncio.wait_for(
                        asyncio.gather(
                            first(),
                            self.checkout(None, 1, "catalog-race-" + method, method),
                        ),
                        15,
                    )
                self.assertEqual([r.status_code for r in responses], [200, 200])
                self.assertEqual(responses[0].json(), responses[1].json())

    async def test_ambiguous_invoice_is_not_recreated_after_provider_retry_window(self):
        key = "expired-ambiguous-invoice"
        with patch(
            "app.services.topups.YooKassa.create",
            side_effect=ValueError("Injected timeout"),
        ):
            self.assertEqual(
                (await self.checkout(None, 1, key, "yookassa")).status_code, 409
            )
        operation = (await self.status(key)).json()
        async with self.db.sessions.begin() as session:
            payment = await session.get(Payment, operation["payment_id"])
            payment.created_at = datetime.now(timezone.utc) - timedelta(days=1)
        self.assertEqual(
            (await self.checkout(None, 1, key, "yookassa")).status_code, 409
        )
        self.assertEqual(await self.counts(), (1, 0, 0))
        self.assertEqual(self.attempts, [])

    async def test_other_channel_can_finish_invoice_before_website_response_recovery(
        self,
    ):
        for status in ("succeeded", "canceled"):
            with self.subTest(status=status):
                key = "other-channel-" + status
                with patch(
                    "app.services.topups.YooKassa.create",
                    side_effect=ValueError("Injected timeout"),
                ):
                    self.assertEqual(
                        (await self.checkout(None, 1, key, "yookassa")).status_code, 409
                    )
                pid = (await self.status(key)).json()["payment_id"]
                async with self.db.sessions.begin() as session:
                    repo = SQLRepository(session)
                    service = SubscriptionCheckoutService(repo, self.settings)
                    await service.render(f"order_pay:{pid}", self.user)
                    payment = await session.get(Payment, pid)
                    data = dict(
                        id=payment.provider_id,
                        status=status,
                        paid=True,
                        test=True,
                        amount=dict(value="100.00", currency="RUB"),
                        metadata=dict(
                            freeman_payment=str(pid),
                            freeman_user=str(self.uid),
                            freeman_kind="subscription",
                            freeman_months="1",
                            freeman_sid="0",
                        ),
                    )
                    await service.credit(payment, data)
                replay = await self.checkout(None, 1, key, "yookassa")
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertEqual(
                    replay.json()["status"],
                    "paid" if status == "succeeded" else "canceled",
                )
                self.assertEqual(replay.json()["payment_id"], pid)
                if status == "succeeded":
                    self.assertTrue(replay.json()["key_id"])
                self.assertEqual(
                    (await self.checkout(None, 1, key, "yookassa")).json(),
                    replay.json(),
                )
        self.assertEqual(await self.counts(), (2, 1, 1))
        self.assertEqual(len(self.provisioned), 1)

    async def test_same_key_is_scoped_to_customer_for_balance_payments(self):
        key = "same-key-two-customers"
        first = await self.checkout(None, 1, key)
        alice = self.user
        async with self.db.sessions.begin() as session:
            self.user = await session.get(User, self.other)
            session.add(
                BalanceTransaction(
                    user_id=self.other,
                    type="fixture",
                    amount=100_000,
                    description="Fixture",
                )
            )
        self.assertEqual((await self.status(key)).status_code, 404)
        second = await self.checkout(None, 1, key)
        self.assertEqual([first.status_code, second.status_code], [200, 200])
        self.assertNotEqual(
            first.json()["subscription_id"], second.json()["subscription_id"]
        )
        self.user = alice
        self.assertEqual((await self.status(key)).json()["result"], first.json())
        self.assertEqual(await self.counts(), (2, 2, 2))

    async def test_legacy_result_remains_readable_and_cannot_be_rebound(self):
        key = "historical-request-key"
        result = {
            "status": "paid",
            "subscription_id": 123,
            "key_id": 456,
            "amount": 10_000,
        }
        async with self.db.sessions.begin() as session:
            await SQLRepository(session).set_setting(
                f"web_checkout:{self.uid}:{key}", json.dumps(result)
            )
        self.assertEqual((await self.status(key)).json()["result"], result)
        self.assertEqual((await self.checkout(None, 3, key)).status_code, 409)
        self.assertEqual(await self.counts(), (0, 0, 0))

    async def test_cancel_before_delayed_original_request_prevents_purchase(self):
        key = "cancel-delayed-request"
        body = dict(months=1, method="balance", idempotency_key=key)
        canceled = await self.client.post("/api/web/checkout/cancel", json=body)
        self.assertEqual(canceled.status_code, 200)
        delayed = await self.client.post("/api/web/checkout", json=body)
        self.assertEqual(delayed.json(), {"status": "canceled"})
        self.assertEqual(await self.counts(), (0, 0, 0))

    async def test_cancel_never_discards_pending_or_completed_payment(self):
        for method in ("balance", "yookassa"):
            with self.subTest(method=method):
                key = "cannot-abandon-" + method
                first = await self.checkout(None, 1, key, method)
                canceled = await self.client.post(
                    "/api/web/checkout/cancel",
                    json=dict(months=1, method=method, idempotency_key=key),
                )
                self.assertEqual(canceled.json()["result"], first.json())
        self.assertEqual(await self.counts(), (2, 1, 1))
