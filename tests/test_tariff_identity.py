"""F03 PostgreSQL contracts. No real billing, VPN, bot or production access."""

import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from test_billing_concurrency import BillingConcurrency
from test_checkout_idempotency import CheckoutIdempotency

from app.database.models import (
    AccessKey,
    Payment,
    Subscription,
    Tariff,
)
from app.database.repositories import SQLRepository
from app.database.tariff_identity_migration import upgrade
from app.services.admin import AdminService
from app.services.billing import BillingService
from app.services.domain import seed
from app.services.integrations import MockPaymentService, MockProvisioningService
from app.services.pricing import quote
from app.services.screens import ScreenService
from app.services.stars import StarService
from app.services.topups import SubscriptionCheckoutService
from app.web.catalog import catalog_router


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class TariffIdentity(unittest.IsolatedAsyncioTestCase):
    subscription = CheckoutIdempotency.subscription
    ordered_pair = BillingConcurrency.ordered_pair

    def receipt(self, payment, order):
        return dict(
            id=payment.provider_id,
            status="succeeded",
            paid=True,
            test=True,
            amount=dict(value=f"{payment.amount / 100:.2f}", currency="RUB"),
            metadata=dict(
                freeman_payment=str(payment.id),
                freeman_user=str(self.uid),
                freeman_kind="subscription",
                freeman_months=str(order["months"]),
                freeman_sid=str(order["sid"]),
            ),
        )

    async def asyncSetUp(self):
        await CheckoutIdempotency.asyncSetUp(self)
        async with self.db.sessions.begin() as s:
            self.original = (await SQLRepository(s).tariff(1)).id
            await SQLRepository(s).set_setting("legacy_tariff:1", str(self.original))
            other = Tariff(months=1, price=22_000, device_limit=5)
            s.add(other)
            await s.flush()
            self.family = other.id

    async def post(
        self, tid, key="tariff-identity-request", sid=None, method="balance", **extra
    ):
        return await self.client.post(
            "/api/web/checkout",
            json={
                "tariff_id": tid,
                "months": 1,
                "subscription_id": sid,
                "method": method,
                "idempotency_key": key,
                **extra,
            },
        )

    async def admin_client(self):
        settings = self.settings.model_copy(
            update={"admin_ids": str(self.user.telegram_id)}
        )

        async def current(_token):
            return self.user

        app = FastAPI()
        app.include_router(catalog_router(self.db, settings, current))
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://example.test"
        )
        self.addAsyncCleanup(client.aclose)
        return client, settings

    def change(self, **extra):
        return dict(
            kind="vpn",
            name="Family",
            price="220",
            months=1,
            devices=5,
            confirmed=True,
            request_id="f03-admin-request",
            **extra,
        )

    async def test_database_accepts_same_duration_and_different_devices_prices(self):
        async with self.db.sessions() as s:
            plans = list(
                await s.scalars(
                    select(Tariff).where(Tariff.months == 1).order_by(Tariff.id)
                )
            )
            self.assertEqual(
                [(t.id, t.price, t.device_limit) for t in plans],
                [(self.original, 10_000, 1), (self.family, 22_000, 5)],
            )

    async def test_admin_create_replay_edit_and_archive_are_independent(self):
        client, _ = await self.admin_client()
        created = await client.post("/api/web/admin/catalog", json=self.change())
        self.assertEqual(created.status_code, 200, created.text)
        tid = created.json()["id"]
        repeated = await client.post("/api/web/admin/catalog", json=self.change())
        self.assertEqual(repeated.json(), created.json())
        body = self.change(id=tid, version=1)
        body.update(price="280", devices=7, request_id="f03-admin-edit")
        edited = await client.post("/api/web/admin/catalog", json=body)
        self.assertEqual(edited.status_code, 200, edited.text)
        archived = await client.delete(
            f"/api/web/admin/catalog/vpn/{tid}", params={"version": 2}
        )
        self.assertEqual(archived.status_code, 200, archived.text)
        async with self.db.sessions() as s:
            repo = SQLRepository(s)
            self.assertIsNone(await repo.tariff_by_id(tid))
            original = await repo.tariff_by_id(self.original)
            self.assertEqual((original.price, original.device_limit), (10_000, 1))
            self.assertEqual(
                await repo.tariff_setting(original, "tariff_enabled", "1"), "1"
            )
            self.assertEqual(
                await repo.setting(f"tariff_enabled:id{tid}"),
                "0",
            )

    async def test_admin_rejects_editing_duration_to_preserve_history(self):
        client, _ = await self.admin_client()
        body = self.change(id=self.family)
        body.update(months=3)
        response = await client.post("/api/web/admin/catalog", json=body)
        self.assertEqual(response.status_code, 409)

    async def test_legacy_settings_and_personal_price_do_not_leak_to_new_plan(self):
        async with self.db.sessions.begin() as s:
            repo = SQLRepository(s)
            await repo.set_setting("tariff_name:1", "Original")
            await repo.set_setting(f"client_price:{self.uid}:1", "1234")
            await repo.set_setting("tariff_enabled:1", "0")
            old, new = (
                await repo.tariff_by_id(self.original),
                await repo.tariff_by_id(self.family),
            )
            self.assertEqual(await repo.tariff_setting(old, "tariff_name"), "Original")
            self.assertEqual(
                await repo.tariff_setting(new, "tariff_name", "Family"), "Family"
            )
            self.assertEqual((await quote(repo, self.uid, old))[0], 1234)
            self.assertEqual((await quote(repo, self.uid, new))[0], 22_000)
            self.assertEqual(await repo.tariff_setting(new, "tariff_enabled", "1"), "1")
            await repo.set_tariff_setting(new, f"client_price:{self.uid}", "4567")
            self.assertEqual((await quote(repo, self.uid, new))[0], 4567)
            self.assertEqual((await quote(repo, self.uid, old))[0], 1234)

    async def test_select_by_id_and_reject_inconsistent_duration(self):
        result = await self.post(self.family)
        self.assertEqual(result.status_code, 200, result.text)
        async with self.db.sessions() as s:
            sub = await s.get(Subscription, result.json()["subscription_id"])
            self.assertEqual((sub.tariff_id, sub.device_limit), (self.family, 5))
            self.assertEqual(await SQLRepository(s).balance(self.uid), 78_000)
        mismatch = await self.post(self.family, "tariff-wrong-duration", months=3)
        self.assertEqual(mismatch.status_code, 404)

    async def test_id_only_request_and_different_plan_reusing_key_conflict(self):
        response = await self.client.post(
            "/api/web/checkout",
            json={
                "tariff_id": self.family,
                "method": "balance",
                "idempotency_key": "tariff-id-only-request",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        conflict = await self.client.post(
            "/api/web/checkout",
            json={
                "tariff_id": self.original,
                "method": "balance",
                "idempotency_key": "tariff-id-only-request",
            },
        )
        self.assertEqual(conflict.status_code, 409)

    async def test_concurrent_identical_id_checkout_debits_and_provisions_once(self):
        replies = await asyncio.gather(*[self.post(self.family) for _ in range(2)])
        self.assertEqual([r.status_code for r in replies], [200, 200])
        self.assertEqual(replies[0].json(), replies[1].json())
        async with self.db.sessions() as s:
            self.assertEqual(
                await s.scalar(select(func.count()).select_from(Payment)), 1
            )
            self.assertEqual(
                await s.scalar(select(func.count()).select_from(AccessKey)), 1
            )
            self.assertEqual(await SQLRepository(s).balance(self.uid), 78_000)

    async def test_renew_same_duration_different_plan_uses_requested_device_limit(self):
        sid, _, expiry = await self.subscription()
        result = await self.post(self.family, sid=sid)
        self.assertEqual(result.status_code, 200, result.text)
        async with self.db.sessions() as s:
            sub = await s.get(Subscription, sid)
            self.assertEqual((sub.tariff_id, sub.device_limit), (self.family, 5))
            self.assertEqual(
                (sub.expires_at.year, sub.expires_at.month, sub.expires_at.day),
                (expiry.year, 2, 29),
            )

    async def test_legacy_duration_checkout_remains_bound_to_original_tariff(self):
        result = await self.client.post(
            "/api/web/checkout",
            json={
                "months": 1,
                "method": "balance",
                "idempotency_key": "legacy-duration-purchase",
            },
        )
        self.assertEqual(result.status_code, 200, result.text)
        async with self.db.sessions() as s:
            sub = await s.get(Subscription, result.json()["subscription_id"])
            self.assertEqual((sub.tariff_id, sub.device_limit), (self.original, 1))

    async def test_ambiguous_duration_without_legacy_mapping_never_picks_first(self):
        async with self.db.sessions.begin() as s:
            await SQLRepository(s).set_setting("legacy_tariff:1", "")
            self.assertIsNone(await SQLRepository(s).tariff(1))
        result = await self.client.post(
            "/api/web/checkout",
            json={
                "months": 1,
                "method": "balance",
                "idempotency_key": "ambiguous-duration-purchase",
            },
        )
        self.assertEqual(result.status_code, 404)
        self.assertEqual(self.provisioned, [])

    async def test_pending_yookassa_order_keeps_tariff_id_and_paid_terms_after_edit(
        self,
    ):
        sid, _, _ = await self.subscription()
        result = await self.post(self.family, sid=sid, method="yookassa")
        self.assertEqual(result.status_code, 200, result.text)
        pid = result.json()["payment_id"]
        async with self.db.sessions.begin() as s:
            repo = SQLRepository(s)
            p = await s.get(Payment, pid)
            order = json.loads(await repo.setting(f"order:{pid}"))
            self.assertEqual(
                (order["tariff_id"], order["months"], order["devices"]),
                (self.family, 1, 5),
            )
            t = await repo.tariff_by_id(self.family)
            t.price, t.device_limit = 99_000, 9
            await repo.set_tariff_setting(t, "tariff_enabled", "0")
            data = self.receipt(p, order)
            self.assertTrue(
                await SubscriptionCheckoutService(repo, self.settings).credit(p, data)
            )
            self.assertTrue(
                await SubscriptionCheckoutService(repo, self.settings).credit(p, data)
            )
            sub = await s.get(Subscription, sid)
            self.assertEqual(
                (sub.tariff_id, sub.device_limit, p.amount), (self.family, 5, 22_000)
            )

    async def test_historical_paid_order_without_id_uses_frozen_legacy_binding(self):
        sid, _, _ = await self.subscription()
        async with self.db.sessions.begin() as s:
            repo = SQLRepository(s)
            service = SubscriptionCheckoutService(repo, self.settings)
            p = await service.create_order(self.user, 1, sid=sid)
            order = await service.details(p)
            order.pop("tariff_id")
            await repo.set_setting(f"order:{p.id}", json.dumps(order))
            p.status = "order_pending"
            data = self.receipt(p, order)
            self.assertTrue(await service.credit(p, data))
            sub = await s.get(Subscription, sid)
            self.assertEqual((sub.tariff_id, sub.device_limit), (self.original, 1))

    async def test_bot_callbacks_and_balance_preview_use_id(self):
        async with self.db.sessions.begin() as s:
            repo = SQLRepository(s)
            screens = ScreenService(
                repo, self.settings, MockPaymentService(), MockProvisioningService()
            )
            menu = await screens.render("buy", self.user)
            targets = [target for row in menu.rows for _, target in row]
            self.assertIn(f"tariff:id{self.original}", targets)
            self.assertIn(f"tariff:id{self.family}", targets)
            screen = await screens.render(f"tariff:id{self.family}", self.user)
            self.assertIn(
                f"pay_balance:id{self.family}",
                [t for row in screen.rows for _, t in row],
            )
            action = await BillingService(repo, self.settings).preview(
                self.user, f"id{self.family}", 0
            )
        async with self.db.sessions.begin() as s:
            await BillingService(SQLRepository(s), self.settings).render(
                action, self.user
            )
            sub = await s.scalar(select(Subscription))
            self.assertEqual((sub.tariff_id, sub.device_limit), (self.family, 5))

    async def test_bot_admin_add_edit_and_toggle_do_not_target_sibling(self):
        _, settings = await self.admin_client()
        async with self.db.sessions.begin() as s:
            repo = SQLRepository(s)
            admin = AdminService(repo, settings)
            await admin.apply_input(
                "admin:input:tariff_add", "Second | 1 | 330 | 8", self.user
            )
            t = await s.scalar(select(Tariff).where(Tariff.device_limit == 8))
            await admin.apply_input(
                f"admin:input:tariff_price:id{t.id}", "350", self.user
            )
            confirmation = await admin.render(
                f"admin:confirm:tarifftoggle:id{t.id}", self.user
            )
            target = confirmation.rows[0][0][1]
            await admin.render(target, self.user)
            self.assertEqual(t.price, 35_000)
            self.assertEqual(await repo.tariff_setting(t, "tariff_enabled"), "0")
            self.assertEqual(
                await repo.tariff_setting(
                    await repo.tariff_by_id(self.original), "tariff_enabled", "1"
                ),
                "1",
            )

    async def test_stars_invoice_snapshot_uses_unique_id(self):
        async with self.db.sessions.begin() as s:
            repo = SQLRepository(s)
            await repo.set_setting("payment_provider:stars", "1")
            t = await repo.tariff_by_id(self.family)
            await repo.set_tariff_setting(t, "stars_price", "100")
            invoice = await StarService(repo, self.settings).create(
                self.user, f"id{self.family}"
            )
            pid = int(invoice["payload"].split(":")[1])
            order = json.loads(await repo.setting(f"stars_order:{pid}"))
            self.assertEqual(
                (order["tariff_id"], order["months"], order["devices"]),
                (self.family, 1, 5),
            )
            self.assertNotIn("id" + str(self.family), invoice["title"])

    async def test_autorenew_stores_id_not_just_months(self):
        sid, _, _ = await self.subscription()
        async with self.db.sessions.begin() as s:
            repo = SQLRepository(s)
            # A normal same-device alternative, independent of the original.
            t = await repo.add(Tariff(months=1, price=15_000, device_limit=1))
            await BillingService(repo, self.settings).render(
                f"autorenew:{sid}:on:id{t.id}", self.user
            )
            self.assertEqual((await repo.auto_tariff(sid)).id, t.id)
            self.assertEqual(await repo.setting(f"auto_renew_months:{sid}"), "1")

    async def test_seed_does_not_add_defaults_when_duration_has_multiple_plans(self):
        async with self.db.sessions.begin() as s:
            repo = SQLRepository(s)
            await seed(repo, self.settings)
            self.assertEqual(
                await s.scalar(
                    select(func.count()).select_from(Tariff).where(Tariff.months == 1)
                ),
                2,
            )

    async def test_postgresql_migration_preserves_rows_foreign_keys_and_legacy_settings(
        self,
    ):
        sid, _, expiry = await self.subscription()
        async with self.db.sessions.begin() as s:
            (await s.get(Subscription, sid)).tariff_id = self.original
            repo = SQLRepository(s)
            await s.delete(await s.get(Tariff, self.family))
            await repo.set_setting("legacy_tariff:1", "")
            await repo.set_setting("tariff_name:1", "Legacy")
            # Simulate the installed constraint with a non-default name.
            await s.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE tariffs ADD CONSTRAINT old_duration_rule UNIQUE (months)"
                )
            )
        with self.assertRaises(IntegrityError):
            async with self.db.sessions.begin() as s:
                s.add(Tariff(months=1, price=99, device_limit=2))
                await s.flush()
        # Empty markers are not valid migration bindings; installed DB has none.
        async with self.db.sessions.begin() as s:
            await s.execute(
                __import__("sqlalchemy").text(
                    "DELETE FROM app_settings WHERE key='legacy_tariff:1'"
                )
            )
        await upgrade(self.db.engine)
        async with self.db.sessions.begin() as s:
            repo = SQLRepository(s)
            old = await repo.tariff(1)
            self.assertEqual(
                (old.id, old.price, old.device_limit), (self.original, 10_000, 1)
            )
            self.assertEqual(await repo.tariff_setting(old, "tariff_name"), "Legacy")
            s.add(Tariff(months=1, price=33_000, device_limit=8))
        await upgrade(self.db.engine)
        async with self.db.engine.connect() as c:
            unique = await c.run_sync(
                lambda sync: inspect(sync).get_unique_constraints("tariffs")
            )
            self.assertFalse(any(x["column_names"] == ["months"] for x in unique))
        async with self.db.sessions() as s:
            self.assertEqual((await SQLRepository(s).tariff(1)).id, self.original)
            subscription = await s.get(Subscription, sid)
            self.assertEqual(subscription.tariff_id, self.original)
            self.assertEqual(subscription.expires_at, expiry)

    async def test_guest_catalog_and_draft_use_unique_tariff_ids(self):
        from app.web.guest_purchase import guest_router

        app = FastAPI()
        app.include_router(
            guest_router(
                self.db, self.settings, lambda *a: "test-session", "freeman_session"
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://example.test"
        ) as client:
            catalog = await client.get("/api/buy/catalog")
            rows = catalog.json()["tariffs"]
            self.assertEqual(
                {r["id"] for r in rows if r["months"] == 1},
                {self.original, self.family},
            )
            nonce = "guest-tariff-identity-" + "a" * 32
            with patch("app.web.guest_purchase.available", return_value=True):
                draft = await client.post(
                    "/api/buy/draft",
                    json={"tariff_id": self.family, "months": 1, "nonce": nonce},
                )
                self.assertEqual(draft.status_code, 200, draft.text)
                repeat = await client.post(
                    "/api/buy/draft",
                    json={"tariff_id": self.family, "months": 1, "nonce": nonce},
                )
                self.assertEqual(repeat.json(), draft.json())
                changed = await client.post(
                    "/api/buy/draft",
                    json={"tariff_id": self.original, "months": 1, "nonce": nonce},
                )
                self.assertEqual(changed.status_code, 409)
            async with self.db.sessions() as session:
                self.assertEqual(
                    await SQLRepository(session).setting(
                        f"guest_tariff:{draft.json()['order_id']}"
                    ),
                    str(self.family),
                )

    async def test_migration_removes_standalone_unique_duration_index_only(self):
        async with self.db.sessions.begin() as session:
            await session.delete(await session.get(Tariff, self.family))
            await session.execute(
                __import__("sqlalchemy").text(
                    "CREATE UNIQUE INDEX old_months_index ON tariffs(months)"
                )
            )
            await session.execute(
                __import__("sqlalchemy").text(
                    "CREATE INDEX keep_price_index ON tariffs(price)"
                )
            )
        await upgrade(self.db.engine)
        async with self.db.engine.connect() as connection:
            indexes = await connection.run_sync(
                lambda c: inspect(c).get_indexes("tariffs")
            )
            self.assertIn("keep_price_index", {x["name"] for x in indexes})
            self.assertNotIn("old_months_index", {x["name"] for x in indexes})

    async def test_concurrent_distinct_same_duration_renewals_old_then_new(self):
        await self.concurrent_renewal(self.original, self.family, expired=False)

    async def test_concurrent_distinct_same_duration_renewals_new_then_old(self):
        await self.concurrent_renewal(self.family, self.original, expired=False)

    async def test_expired_subscription_concurrent_same_duration_renewals(self):
        await self.concurrent_renewal(self.family, self.original, expired=True)

    async def concurrent_renewal(self, first, second, expired):
        sid, _, _ = await self.subscription(expired)
        replies = await self.ordered_pair(
            sid,
            lambda: self.post(first, "f03-concurrent-first", sid),
            lambda: self.post(second, "f03-concurrent-second", sid),
        )
        self.assertEqual([r.status_code for r in replies], [200, 200])
        async with self.db.sessions() as session:
            sub = await session.get(Subscription, sid)
            self.assertEqual(
                (sub.tariff_id, sub.device_limit),
                (second, 5 if second == self.family else 1),
            )
            self.assertEqual(
                (sub.expires_at.year, sub.expires_at.month, sub.expires_at.day),
                (2027 if expired else 2028, 3, 28 if expired else 29),
            )
            self.assertEqual(await SQLRepository(session).balance(self.uid), 68_000)
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(Payment)), 2
            )

    async def test_admin_grant_id_and_legacy_confirmation_remain_compatible(self):
        from app.database.models import AdminAudit
        from app.services.admin_subscription import grant, preview

        _, settings = await self.admin_client()
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            for selector, expected in (
                (f"id{self.family}", self.family),
                (1, self.original),
            ):
                callback = await preview(repo, self.user, selector)
                _, ref, sid, token = callback.split(":")
                result = await grant(repo, settings, self.user, ref, int(sid), token)
                self.assertEqual(
                    await grant(repo, settings, self.user, ref, int(sid), token), result
                )
                sub = await session.get(Subscription, result)
                self.assertEqual(sub.tariff_id, expected)
            entries = list(
                await session.scalars(
                    select(AdminAudit).where(AdminAudit.action == "admin_free")
                )
            )
            self.assertEqual(len(entries), 2)
            self.assertTrue(all("срок 1 мес." in a.details for a in entries))
