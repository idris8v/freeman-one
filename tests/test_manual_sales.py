"""Manual accounting uses real isolated PostgreSQL and mocked RemnaWave only."""

import asyncio
import json
import os
import unittest
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
import test_vless_creation as creation
import test_vless_renewal as renewal
from app.services import manual_sales
from app.database.models import Payment, Subscription, Tariff
from app.database.repositories import SQLRepository
from app.web.admin_dashboard import admin_dashboard_router
from app.web.admin_reports import build_report
from app.web.admin_payments import build_payments, parameters


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class ManualSales(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await creation.VlessCreationConsistency.asyncSetUp(self)
        self.settings.admin_ids = "123"
        clock = patch("app.services.manual_sales.now", return_value=renewal.NOW)
        clock.start()
        self.addCleanup(clock.stop)

        async def current(_):
            return self.user

        self.client._transport.app.include_router(
            admin_dashboard_router(self.db, self.settings, current)
        )
        async with self.db.sessions() as session:
            self.tid = await session.scalar(select(Tariff.id).where(Tariff.months == 1))
        self.body = dict(
            user_id=self.uid,
            days=30,
            device_limit=1,
            tariff_id=self.tid,
            expected_amount=10000,
            issuance_kind="manual_sale",
            payment_received=False,
            request_id="manual-sale-fixture",
        )

    async def grant(self, **changes):
        return await self.client.post(
            "/api/web/admin/keys/grant", json=self.body | changes
        )

    async def snapshot(self):
        async with self.db.sessions() as session:
            rows = list(await session.scalars(select(Payment).order_by(Payment.id)))
            return rows, [
                json.loads(
                    await SQLRepository(session).setting(f"sale_snapshot:{p.id}")
                )
                for p in rows
            ]

    async def test_unpaid_received_price_snapshot_and_reports(self):
        response = await self.grant()
        self.assertEqual(response.status_code, 200, response.text)
        pid = response.json()["payment_id"]
        async with self.db.sessions.begin() as session:
            t = await session.get(Tariff, self.tid)
            t.price = 90000
            self.assertEqual(await SQLRepository(session).balance(self.uid), 100000)
        payments, snapshots = await self.snapshot()
        self.assertEqual(
            (payments[0].amount, payments[0].status, snapshots[0]["tariff_id"]),
            (10000, "manual_unpaid", self.tid),
        )
        async with self.db.sessions() as session:
            report = await build_report(
                session,
                renewal.NOW - timedelta(days=1),
                renewal.NOW + timedelta(days=1),
            )
            self.assertEqual(
                (
                    report["stats"]["revenue"],
                    report["manual"]["gross"],
                    report["manual"]["unpaid"],
                ),
                (0, 10000, 10000),
            )
            ledger = await build_payments(
                session,
                parameters(
                    renewal.NOW - timedelta(days=1), renewal.NOW + timedelta(days=1)
                ),
            )
            sale = next(r for r in ledger["items"] if r["id"] == f"P{pid}")
            self.assertTrue(sale["can_confirm_received"])
            self.assertEqual(sale["external"], 0)
        for _ in range(2):
            r = await self.client.post(
                f"/api/web/admin/manual-sales/payments/{pid}/received"
            )
            self.assertEqual(r.status_code, 200, r.text)
        async with self.db.sessions() as session:
            report = await build_report(
                session,
                renewal.NOW - timedelta(days=1),
                renewal.NOW + timedelta(days=1),
            )
            self.assertEqual(
                (
                    report["stats"]["revenue"],
                    report["manual"]["collected"],
                    report["manual"]["unpaid"],
                ),
                (10000, 10000, 0),
            )
            self.assertEqual(report["tariffs"][0]["tariff_id"], self.tid)
        again = await self.grant()
        self.assertEqual(again.json()["key_id"], response.json()["key_id"])
        self.assertEqual(len(self.api.creates), 1)

    async def test_concurrent_duplicate_is_one_payment_and_key(self):
        a, b = await asyncio.gather(
            self.grant(payment_received=True), self.grant(payment_received=True)
        )
        self.assertEqual((a.status_code, b.status_code), (200, 200), (a.text, b.text))
        self.assertEqual(a.json(), b.json())
        p, _ = await self.snapshot()
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0].status, "manual_paid")
        self.assertEqual(len(self.api.creates), 1)
        r = await self.grant(name="different", payment_received=True)
        self.assertEqual(r.status_code, 409, r.text)

    async def test_complimentary_and_test_have_no_fake_payment(self):
        for category in ("complimentary", "test"):
            r = await self.grant(
                issuance_kind=category,
                tariff_id=None,
                days=7,
                device_limit=2,
                expected_amount=None,
                request_id="category-" + category,
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["amount"], 0)
        p, _ = await self.snapshot()
        self.assertEqual(p, [])
        self.assertEqual(len(self.api.creates), 2)

    async def test_invalid_price_or_terms_before_mutation(self):
        for changes in (
            dict(expected_amount=1),
            dict(days=31),
            dict(device_limit=True),
            dict(issuance_kind="test", payment_received=True),
        ):
            r = await self.grant(**changes)
            self.assertIn(r.status_code, (409, 422), r.text)
        p, _ = await self.snapshot()
        self.assertEqual(p, [])
        self.assertEqual(self.api.creates, [])
        async with self.db.sessions() as session:
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(Subscription)), 0
            )

    async def test_lost_remote_response_reuses_frozen_sale(self):
        self.api.lose_response = True
        first = await self.grant(payment_received=True)
        self.assertIn(first.status_code, (422, 503), first.text)
        p, _ = await self.snapshot()
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0].status, "manual_received_pending")
        async with self.db.sessions.begin() as session:
            (await session.get(Tariff, self.tid)).price = 99900
        self.api.lookup_error = False
        self.api.lose_response = False
        second = await self.grant(payment_received=True)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["amount"], 10000)
        self.assertEqual(len(self.api.creates), 1)

    async def test_failed_final_commit_recovers_original_key_and_payment(self):
        armed = True

        def fail(session):
            nonlocal armed
            if armed and session.info.pop("manual_commit_failure", False):
                armed = False
                raise SQLAlchemyError("injected final commit loss")

        real_finish = manual_sales.finish

        async def finish(repo, *args):
            result = await real_finish(repo, *args)
            repo.session.info["manual_commit_failure"] = True
            return result

        event.listen(Session, "before_commit", fail)
        try:
            with patch("app.services.manual_sales.finish", side_effect=finish):
                r = await self.grant()
                self.assertEqual(r.status_code, 503, r.text)
        finally:
            event.remove(Session, "before_commit", fail)
        r = await self.grant()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.api.creates), 1)
        p, _ = await self.snapshot()
        self.assertEqual(len(p), 1)

    async def test_customer_discount_and_history_are_frozen(self):
        async with self.db.sessions.begin() as session:
            await SQLRepository(session).set_setting(
                f"client_discount:{self.uid}", "20"
            )
        r = await self.grant(expected_amount=8000, payment_received=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["amount"], 8000)
        from app.web.customer_profile import section

        async with self.db.sessions() as session:
            from app.database.models import User

            history = await section(
                SQLRepository(session),
                self.settings,
                await session.get(User, self.uid),
                "activity",
                1,
                10,
            )
            events = [
                v for v in history["items"] if v["title"] == "Выдача / продление VPN"
            ]
            self.assertEqual(len(events), 1)
            self.assertIn("80.00", events[0]["detail"])

    async def test_explicit_unlimited_tariff_keeps_numeric_zero(self):
        async with self.db.sessions.begin() as session:
            (await session.get(Tariff, self.tid)).device_limit = 0
        r = await self.grant(device_limit=0)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.api.creates[0]["hwidDeviceLimit"], 0)

    async def test_nonowner_cannot_issue_or_confirm_receipt(self):
        self.settings.admin_ids = ""
        self.assertEqual((await self.grant()).status_code, 403)
        self.assertEqual(
            (
                await self.client.post(
                    "/api/web/admin/manual-sales/payments/1/received"
                )
            ).status_code,
            403,
        )
        self.assertEqual((await self.snapshot())[0], [])


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class ManualRenewals(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await renewal.VlessRenewalConsistency.asyncSetUp(self)
        self.settings.admin_ids = "123"
        clock = patch("app.services.manual_sales.now", return_value=renewal.NOW)
        clock.start()
        self.addCleanup(clock.stop)

        async def current(_):
            return self.user

        self.client._transport.app.include_router(
            admin_dashboard_router(self.db, self.settings, current)
        )
        async with self.db.sessions() as session:
            self.tid = await session.scalar(select(Tariff.id).where(Tariff.months == 1))
        self.body = dict(
            action="extend",
            value="30",
            unit="days",
            confirmed=True,
            issuance_kind="manual_sale",
            tariff_id=self.tid,
            payment_confirmed=True,
            expected_amount=10000,
            request_id="manual-renew-fixture",
        )

    async def post(self, **changes):
        with patch("app.services.manual_sales.now", return_value=renewal.NOW):
            return await self.client.post(
                f"/api/web/admin/keys/{self.kid}/action", json=self.body | changes
            )

    async def test_two_sales_extend_twice_duplicate_once(self):
        a, b = await asyncio.gather(
            self.post(), self.post(request_id="manual-second-fixture")
        )
        self.assertEqual((a.status_code, b.status_code), (200, 200), (a.text, b.text))
        c = await self.post()
        self.assertEqual(c.status_code, 200, c.text)
        async with self.db.sessions() as session:
            sub = await session.get(Subscription, self.sid)
            from app.services.billing import extend

            self.assertEqual(sub.expires_at, extend(extend(renewal.NOW, 1), 1))
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(Payment)), 2
            )
            self.assertEqual(await SQLRepository(session).balance(self.uid), 100000)
        self.assertEqual(len(self.api.patches), 2)

    async def test_partial_enable_failure_recovers_no_second_accounting(self):
        self.api.enable_error = 503
        first = await self.post()
        self.assertEqual(first.status_code, 409, first.text)
        self.api.enable_error = None
        second = await self.post()
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(len(self.api.patches), 1)
        async with self.db.sessions() as session:
            p = list(await session.scalars(select(Payment)))
            self.assertEqual(len(p), 1)
            self.assertEqual(p[0].status, "manual_paid")

    async def test_changed_tariff_limits_recovered_without_usage_reset(self):
        async with self.db.sessions.begin() as session:
            tariff = await session.scalar(select(Tariff).where(Tariff.months == 3))
            tid = tariff.id
            await SQLRepository(session).set_setting(
                f"tariff_traffic:{tid}", str(5 * 1024**3)
            )
        self.api.remote["usedTrafficBytes"] = 1234
        self.api.enable_error = 503
        terms = dict(tariff_id=tid, value="90", expected_amount=30000)
        first = await self.post(**terms)
        self.assertEqual(first.status_code, 409, first.text)
        async with self.db.sessions.begin() as session:
            await SQLRepository(session).set_setting(f"tariff_traffic:{tid}", "0")
            (await session.get(Tariff, tid)).price = 99900
        self.api.enable_error = None
        second = await self.post(**terms)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(self.api.remote["trafficLimitBytes"], 5 * 1024**3)
        self.assertEqual(self.api.remote["usedTrafficBytes"], 1234)
        self.assertEqual(self.api.remote["hwidDeviceLimit"], 3)
        async with self.db.sessions() as session:
            sub = await session.get(Subscription, self.sid)
            self.assertEqual(sub.tariff_id, tid)
            self.assertEqual(sub.device_limit, 3)
        self.assertEqual(len(self.api.patches), 1)
