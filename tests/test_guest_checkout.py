"""Guest HTTP checkout against disposable PostgreSQL; no real provider calls."""

import asyncio
import os
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select

from test_checkout_idempotency import CheckoutIdempotency
from app.database.models import (
    AccessKey,
    GuestOrder,
    Payment,
    RemnawaveServer,
    User,
    VlessCredential,
)
from app.database.repositories import SQLRepository
from app.services.topups import SubscriptionCheckoutService
from app.web.guest_purchase import guest_router


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class GuestCheckout(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await CheckoutIdempotency.asyncSetUp(self)
        app = FastAPI()
        app.include_router(
            guest_router(
                self.db, self.settings, lambda *a: "fixture", "freeman_session"
            )
        )
        self.guest = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://example.test"
        )
        self.addAsyncCleanup(self.guest.aclose)
        gate = patch("app.web.guest_purchase.available", return_value=True)
        gate.start()
        self.addCleanup(gate.stop)
        async with self.db.sessions() as s:
            self.tid = (await SQLRepository(s).tariff(1)).id

    async def draft(self, **extra):
        return await self.guest.post(
            "/api/buy/draft",
            json=dict(
                months=1,
                tariff_id=self.tid,
                nonce="fixture-guest-intent-" + "a" * 32,
                **extra,
            ),
        )

    async def begin(self):
        r = await self.draft()
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["order_id"]

    async def pay(self, oid):
        return await self.guest.post(
            f"/api/buy/{oid}/pay", json=dict(months=1, without_email=True)
        )

    async def counts(self):
        async with self.db.sessions() as s:
            return tuple(
                [
                    await s.scalar(select(func.count()).select_from(m))
                    for m in (GuestOrder, Payment, User)
                ]
            )

    async def test_lost_provider_response_preserves_committed_payment_and_key(self):
        oid = await self.begin()

        async def lost(*args, **kwargs):
            await self.create(*args, **kwargs)
            raise ValueError("Injected response loss")

        with patch("app.services.topups.YooKassa.create", side_effect=lost):
            first = await self.pay(oid)
        self.assertEqual(first.status_code, 422, first.text)
        self.assertEqual((await self.counts())[:2], (1, 1))
        # A new process/client sees the durable order, not a second invoice.
        retry = await self.pay(oid)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(len(self.invoices), 1)
        self.assertEqual(self.attempts[0], self.attempts[1])
        self.assertEqual((await self.counts())[:2], (1, 1))

    async def test_concurrent_pay_and_duplicate_retry_one_invoice(self):
        oid = await self.begin()
        replies = await asyncio.gather(self.pay(oid), self.pay(oid))
        self.assertEqual([r.status_code for r in replies], [200, 200])
        self.assertEqual(replies[0].json(), replies[1].json())
        self.assertEqual(len(self.attempts), 1)
        self.assertEqual(len(self.invoices), 1)
        self.assertEqual((await self.counts())[:2], (1, 1))
        self.assertEqual((await self.pay(oid)).json(), replies[0].json())

    async def test_lost_local_commit_reuses_staged_provider_identifier(self):
        oid = await self.begin()
        original = SQLRepository.set_setting

        async def fail(repo, key, value):
            if key.startswith("order_url:"):
                raise ValueError("Injected transaction failure after remote create")
            return await original(repo, key, value)

        with patch.object(SQLRepository, "set_setting", fail):
            r = await self.pay(oid)
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual((await self.counts())[:2], (1, 1))
        self.assertEqual((await self.pay(oid)).status_code, 200)
        self.assertEqual(len(self.invoices), 1)
        self.assertEqual(self.attempts[0], self.attempts[1])

    async def test_pending_receipt_and_missing_cookie_are_safe(self):
        oid = await self.begin()
        await self.pay(oid)
        with patch(
            "app.services.topups.YooKassa.get",
            return_value={"id": f"fixture-1", "status": "pending"},
        ):
            r = await self.guest.get(f"/api/buy/{oid}/receipt")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "order_pending")
        self.assertNotIn("key", r.json())
        self.guest.cookies.clear()
        self.assertEqual((await self.pay(oid)).status_code, 403)
        self.assertEqual(len(self.invoices), 1)

    async def test_disabled_creation_does_not_mutate_or_call_provider(self):
        before = await self.counts()
        with patch("app.web.guest_purchase.available", return_value=False):
            r = await self.draft()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(await self.counts(), before)
        self.assertFalse(self.attempts)

    async def test_same_nonce_different_tariff_is_conflict(self):
        await self.begin()
        async with self.db.sessions() as s:
            tid = (await SQLRepository(s).tariff(3)).id
        r = await self.guest.post(
            "/api/buy/draft",
            json=dict(
                months=3, tariff_id=tid, nonce="fixture-guest-intent-" + "a" * 32
            ),
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual((await self.counts())[:2], (1, 0))

    async def test_expired_uncertain_invoice_never_creates_second_payment(self):
        from datetime import timedelta
        from app.database.models import now

        oid = await self.begin()
        with patch(
            "app.services.topups.YooKassa.create", side_effect=ValueError("Timeout")
        ):
            await self.pay(oid)
        async with self.db.sessions.begin() as s:
            p = await s.scalar(select(Payment))
            p.created_at = now() - timedelta(hours=25)
        r = await self.pay(oid)
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual((await self.counts())[:2], (1, 1))
        self.assertFalse(self.invoices)

    async def provider_receipt(self):
        async with self.db.sessions() as s:
            p = await s.scalar(select(Payment))
            order = await SubscriptionCheckoutService(
                SQLRepository(s), self.settings
            ).details(p)
            return dict(
                id=f"fixture-{p.id}",
                status="succeeded",
                paid=True,
                test=self.settings.yookassa_test_mode,
                amount=dict(value=f"{p.amount / 100:.2f}", currency="RUB"),
                metadata=dict(
                    freeman_payment=str(p.id),
                    freeman_user=str(p.user_id),
                    freeman_kind="subscription",
                    freeman_months=str(order["months"]),
                    freeman_sid=str(order["sid"]),
                ),
            )

    async def webhook_client(self):
        from app.web.account_management import management_router

        app = FastAPI()
        app.include_router(
            management_router(
                self.db,
                self.settings,
                lambda _: None,
                lambda *a: "fixture",
                "freeman_session",
            )
        )
        c = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://example.test"
        )
        self.addAsyncCleanup(c.aclose)
        return c

    async def test_webhook_recovers_staged_invoice_after_lost_create_response(self):
        oid = await self.begin()

        async def lost(*a, **kw):
            await self.create(*a, **kw)
            raise ValueError("Lost response")

        with patch("app.services.topups.YooKassa.create", side_effect=lost):
            await self.pay(oid)
        data = await self.provider_receipt()
        data["status"] = "pending"
        data["paid"] = False
        c = await self.webhook_client()
        with patch("app.services.topups.YooKassa.get", return_value=data):
            r = await c.post(
                "/api/payments/yookassa/webhook",
                json=dict(event="payment.succeeded", object=dict(id=data["id"])),
            )
        self.assertEqual(r.status_code, 200, r.text)
        async with self.db.sessions() as s:
            p = await s.scalar(select(Payment))
            self.assertEqual((p.status, p.provider_id), ("order_pending", data["id"]))
        self.assertEqual((await self.pay(oid)).status_code, 200)
        self.assertEqual(len(self.invoices), 1)
        self.assertEqual(len(self.attempts), 1)

    async def test_webhook_rejects_wrong_amount_on_uncertain_invoice(self):
        oid = await self.begin()
        with patch(
            "app.services.topups.YooKassa.create",
            side_effect=ValueError("Lost response"),
        ):
            await self.pay(oid)
        data = await self.provider_receipt()
        data["amount"]["value"] = "0.01"
        c = await self.webhook_client()
        with patch("app.services.topups.YooKassa.get", return_value=data):
            r = await c.post(
                "/api/payments/yookassa/webhook",
                json=dict(event="payment.succeeded", object=dict(id=data["id"])),
            )
        self.assertEqual(r.status_code, 422, r.text)
        async with self.db.sessions() as s:
            p = await s.scalar(select(Payment))
            self.assertEqual(p.status, "order_created")
            self.assertFalse(p.provider_id)

    async def test_paid_receipt_requires_real_key_and_recovers_without_second_charge(
        self,
    ):
        oid = await self.begin()
        await self.pay(oid)
        data = await self.provider_receipt()
        with (
            patch("app.services.topups.YooKassa.get", return_value=data),
            patch(
                "app.services.vless_provision.ensure",
                side_effect=ValueError("Unavailable panel"),
            ),
        ):
            r = await self.guest.get(f"/api/buy/{oid}/receipt")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), dict(status="order_processing", confirmed=True))
        self.assertNotIn("key", r.json())

        async def ensure(repo, settings, sub, user, **kw):
            panel = await repo.add(
                RemnawaveServer(
                    name="Fixture",
                    country="Test",
                    api_url="https://panel.example.test",
                    api_token="not-real",
                )
            )
            key = await repo.add(
                AccessKey(
                    user_id=user.id,
                    name="Purchased",
                    mode="vless",
                    expires_at=sub.expires_at,
                )
            )
            await repo.set_setting(f"subscription_key:{sub.id}", str(key.id))
            return await repo.add(
                VlessCredential(
                    server_id=panel.id,
                    user_id=user.id,
                    subscription_id=sub.id,
                    access_key_id=key.id,
                    remote_user_uuid="fixture-user",
                    subscription_url="https://sub.example.test/fixture",
                    expires_at=sub.expires_at,
                    device_limit=sub.device_limit,
                )
            )

        with (
            patch("app.services.topups.YooKassa.get", return_value=data),
            patch("app.services.vless_provision.ensure", side_effect=ensure),
        ):
            r = await self.guest.get(f"/api/buy/{oid}/receipt")
            repeat = await self.guest.get(f"/api/buy/{oid}/receipt")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "order_paid")
        self.assertEqual(r.json()["key"], "https://sub.example.test/fixture")
        self.assertEqual(repeat.json()["key"], r.json()["key"])
        self.assertEqual((await self.counts())[:2], (1, 1))
        self.assertEqual(len(self.attempts), 1)

    async def test_verified_existing_email_reuses_account_without_telegram(self):
        from app.database.models import AccountChallenge

        async with self.db.sessions.begin() as s:
            user = await s.get(User, self.uid)
            user.telegram_id = None
            user.email = "existing@example.test"
            user.email_verified = True
        oid = await self.begin()
        sent = []

        async def send(settings, email, code):
            sent.append(code)

        with patch("app.services.accounts.send_email", side_effect=send):
            challenge = await self.guest.post(
                f"/api/buy/{oid}/email", json=dict(email="existing@example.test")
            )
        self.assertEqual(challenge.status_code, 200, challenge.text)
        r = await self.guest.post(
            f"/api/buy/{oid}/email/verify",
            json=dict(token=challenge.json()["token"], code=sent[0]),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((await self.pay(oid)).status_code, 200)
        async with self.db.sessions() as s:
            row = await s.get(GuestOrder, oid)
            self.assertEqual(row.user_id, self.uid)
            self.assertEqual(await s.scalar(select(func.count()).select_from(User)), 2)
            challenge = await s.scalar(select(AccountChallenge))
            self.assertTrue(challenge.consumed)

    async def test_guest_renewal_preserves_key_and_applies_period_once(self):
        from test_billing_concurrency import BillingConcurrency
        from app.database.models import Subscription

        sid, kid, _expiry = await BillingConcurrency.subscription(self)
        r = await self.draft(key="https://sub.example.test/fixture")
        self.assertEqual(r.status_code, 200, r.text)
        oid = r.json()["order_id"]
        self.assertEqual((await self.pay(oid)).status_code, 200)
        data = await self.provider_receipt()
        with patch("app.services.topups.YooKassa.get", return_value=data):
            first = await self.guest.get(f"/api/buy/{oid}/receipt")
            second = await self.guest.get(f"/api/buy/{oid}/receipt")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["key"], "https://sub.example.test/fixture")
        self.assertEqual(first.json()["key"], second.json()["key"])
        async with self.db.sessions() as s:
            sub = await s.get(Subscription, sid)
            self.assertEqual(sub.expires_at.isoformat(), "2028-02-29T12:00:00+00:00")
        self.assertEqual(len(self.invoices), 1)
        self.assertEqual(len(self.attempts), 1)
