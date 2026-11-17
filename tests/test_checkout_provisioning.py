"""F01 production checkout on isolated PostgreSQL; providers are mocked."""

import asyncio
import json
import os
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_billing_concurrency import NOW, BillingConcurrency
from test_vless_creation import Panel

from app.database.models import (
    AccessKey,
    BalanceTransaction,
    Payment,
    RemnawaveServer,
    Subscription,
    Tariff,
    User,
    VlessCreation,
    VlessCredential,
    VlessReservation,
)
from app.database.repositories import SQLRepository
from app.services import vless_provision
from app.services.billing import prepare_autorenew
from app.services.demo_keys import create_demo_file, demo_enabled, key_screen
from app.services.domain import SubscriptionService
from app.services.topups import reconcile
from app.services.topups import SubscriptionCheckoutService
from app.services.stars import StarService
from app.web.cabinet.keys import connection_data


class DemoPolicy(unittest.TestCase):
    def enabled(self, url, opt_in):
        bind = SimpleNamespace(
            url=make_url(url),
            dialect=SimpleNamespace(name=make_url(url).get_backend_name()),
        )
        return demo_enabled(
            SimpleNamespace(
                session=SimpleNamespace(
                    bind=bind, info={"isolated_demo_provisioning": opt_in}
                )
            )
        )

    def test_disposable_database_alone_does_not_enable_demos(self):
        for url in ("postgresql:///test_checkout", "sqlite:///:memory:"):
            self.assertFalse(self.enabled(url, False))

    def test_opt_in_cannot_enable_demos_on_a_production_database(self):
        for url in ("postgresql:///freeman", "sqlite:///freeman.db"):
            self.assertFalse(self.enabled(url, True))

    def test_opt_in_on_disposable_database_enables_fixture_provisioning(self):
        for url in ("postgresql:///test_checkout", "sqlite:///:memory:"):
            self.assertTrue(self.enabled(url, True))


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class CheckoutProvisioning(unittest.IsolatedAsyncioTestCase):
    checkout = BillingConcurrency.checkout
    paid_order = BillingConcurrency.paid_order
    receipt = BillingConcurrency.receipt

    async def asyncSetUp(self):
        await BillingConcurrency.asyncSetUp(self)
        # Production policy, even though the physical DB is a disposable test DB.
        self.db.sessions.configure(info={})
        self.api = Panel()
        item = patch(
            "app.services.remnawave.RemnawaveService.api",
            side_effect=lambda *_: self.api,
        )
        item.start()
        self.addCleanup(item.stop)

    async def panel(self, **kwargs):
        async with self.db.sessions.begin() as session:
            panel = RemnawaveServer(
                **dict(
                    name="Fixture",
                    country="TEST",
                    api_url="https://panel.example.test",
                    api_token="fixture-only",
                    internal_squad_uuid="fixture-squad",
                    health="online",
                    max_users=10,
                )
                | kwargs
            )
            session.add(panel)
            await session.flush()
            return panel.id

    async def assert_pending(self, order, *, balance=100_000):
        state = await self.state()
        self.assertEqual(
            state["payments"], [(order[0], "order_pending", order[1]["id"], None)]
        )
        self.assertEqual(state["counts"], [0, 0, 0])
        self.assertEqual(state["demos"], {})
        self.assertEqual(state["balance"], balance)

    async def assert_real(self, *, paid_order=None, balance=90_000):
        state = await self.state()
        self.assertEqual(state["counts"], [1, 1, 1])
        self.assertEqual(state["balance"], balance)
        # An empty legacy setting is not a sample configuration.
        self.assertFalse(any(state["demos"].values()))
        self.assertEqual(len(state["payments"]), 1)
        self.assertEqual(len(self.api.remote), 1)
        async with self.db.sessions() as session:
            sub = await session.scalar(select(Subscription))
            key = await session.scalar(select(AccessKey))
            credential = await session.scalar(select(VlessCredential))
            operation = await session.scalar(select(VlessCreation))
            self.assertEqual(key.mode, "vless")
            self.assertEqual(key.status, "active")
            self.assertEqual(sub.expires_at, credential.expires_at)
            self.assertEqual(key.expires_at, credential.expires_at)
            self.assertEqual(credential.subscription_id, sub.id)
            self.assertEqual(credential.access_key_id, key.id)
            self.assertEqual(operation.state, "associated")
            self.assertEqual(operation.credential_id, credential.id)
            if paid_order:
                self.assertEqual(
                    state["payments"],
                    [(paid_order[0], "order_paid", paid_order[1]["id"], sub.id)],
                )

    async def state(self):
        async with self.db.sessions() as session:
            repo = SQLRepository(session)
            return dict(
                balance=await repo.balance(self.uid),
                payments=[
                    (p.id, p.status, p.provider_id, p.subscription_id)
                    for p in await session.scalars(select(Payment).order_by(Payment.id))
                ],
                counts=[
                    await session.scalar(select(func.count()).select_from(model))
                    for model in (Subscription, AccessKey, VlessCredential)
                ],
                demos=await repo.settings_prefix("demo_file:"),
            )

    async def test_missing_panels_reject_balance_without_charge_or_demo(self):
        response = await self.checkout(None, 1, "f01-no-panels-balance")
        self.assertEqual(
            response.status_code, 409, f"{response.text}; {await self.state()}"
        )
        self.assertEqual(
            await self.state(),
            dict(balance=100_000, payments=[], counts=[0, 0, 0], demos={}),
        )

    async def test_invalid_tariff_traffic_rejects_checkout_before_writes_or_provider_calls(
        self,
    ):
        await self.panel()
        async with self.db.sessions.begin() as session:
            tariff = await SQLRepository(session).tariff(1)
            await SQLRepository(session).set_setting(
                f"tariff_traffic:{tariff.id}", "not-a-limit"
            )
        statements = []

        def record(_connection, _cursor, statement, *_args):
            if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
                statements.append(statement)

        event.listen(self.db.engine.sync_engine, "before_cursor_execute", record)
        try:
            with patch(
                "app.services.topups.YooKassa.create", new_callable=AsyncMock
            ) as gateway:
                for method in ("balance", "yookassa"):
                    response = await self.checkout(
                        None, 1, "invalid-config-" + method, method
                    )
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertIn("лимит трафика", response.json()["detail"])
                gateway.assert_not_awaited()
        finally:
            event.remove(self.db.engine.sync_engine, "before_cursor_execute", record)
        self.assertEqual(statements, [])
        self.assertEqual(self.api.lookups, [])
        self.assertEqual(self.api.creates, [])
        self.assertEqual(
            await self.state(),
            dict(balance=100_000, payments=[], counts=[0, 0, 0], demos={}),
        )
        async with self.db.sessions() as session:
            for model in (VlessCreation, VlessReservation):
                self.assertEqual(
                    await session.scalar(select(func.count()).select_from(model)), 0
                )

    async def test_invalid_bank_and_stars_allowances_are_rejected_before_order_mutations(
        self,
    ):
        statements = []

        def record(_connection, _cursor, statement, *_args):
            if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
                statements.append(statement)

        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            tariff = await repo.tariff(1)
            await repo.set_tariff_setting(tariff, "stars_price", "10")
            await repo.set_setting("payment_provider:stars", "1")
            await repo.set_setting(f"tariff_traffic:{tariff.id}", "false")
        event.listen(self.db.engine.sync_engine, "before_cursor_execute", record)
        try:
            for service_type in (SubscriptionCheckoutService, StarService):
                async with self.db.sessions.begin() as session:
                    service = service_type(SQLRepository(session), self.settings)
                    action = (
                        service.create_order
                        if isinstance(service, SubscriptionCheckoutService)
                        else service.create
                    )
                    with self.assertRaisesRegex(ValueError, "лимит трафика"):
                        await action(await session.get(User, self.uid), 1)
        finally:
            event.remove(self.db.engine.sync_engine, "before_cursor_execute", record)
        self.assertEqual(statements, [])
        self.assertEqual(
            await self.state(),
            dict(balance=100_000, payments=[], counts=[0, 0, 0], demos={}),
        )

    async def test_numeric_zero_allowances_remain_supported_for_existing_tariffs(self):
        await self.panel()
        async with self.db.sessions.begin() as session:
            tariff = await session.scalar(select(Tariff).where(Tariff.months == 1))
            tariff.device_limit = 0
        response = await self.checkout(None, 1, "explicit-unlimited-purchase")
        self.assertEqual(response.status_code, 200, response.text)
        async with self.db.sessions() as session:
            credential = await session.scalar(select(VlessCredential))
            self.assertEqual(credential.device_limit, 0)
            self.assertEqual(credential.traffic_limit, 0)
        self.assertEqual(self.api.creates[0]["hwidDeviceLimit"], 0)
        self.assertEqual(self.api.creates[0]["trafficLimitBytes"], 0)

    async def test_paid_order_without_panels_stays_pending_without_demo_activation(
        self,
    ):
        order = await self.paid_order(0, 1)
        with self.assertRaises(ValueError):
            await self.receipt(order)
        await self.assert_pending(order)

    async def test_unsuitable_panels_reject_balance_without_any_provider_call(self):
        pid = await self.panel()
        for invalid in (
            dict(status="disabled"),
            dict(health="offline"),
            dict(internal_squad_uuid=""),
            dict(users_total=10),
        ):
            with self.subTest(panel=invalid):
                async with self.db.sessions.begin() as session:
                    panel = await session.get(RemnawaveServer, pid)
                    for name, value in dict(
                        status="active",
                        health="online",
                        internal_squad_uuid="fixture-squad",
                        users_total=0,
                    ).items():
                        setattr(panel, name, invalid.get(name, value))
                response = await self.checkout(None, 1, "f01-unsuitable")
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual((await self.state())["balance"], 100_000)
                self.assertEqual((await self.state())["payments"], [])
                self.assertEqual((await self.state())["counts"], [0, 0, 0])
        self.assertEqual(self.api.creates, [])
        self.assertEqual(self.api.lookups, [])

    async def test_unreachable_panel_rejects_balance_without_charge(self):
        await self.panel()
        self.api.lookup_error = True
        response = await self.checkout(None, 1, "f01-panel-outage")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            await self.state(),
            dict(balance=100_000, payments=[], counts=[0, 0, 0], demos={}),
        )
        self.assertEqual(self.api.creates, [])

    async def test_failed_create_rejects_balance_without_charge(self):
        await self.panel()
        self.api.creation_error = 401
        response = await self.checkout(None, 1, "f01-create-rejection")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            await self.state(),
            dict(balance=100_000, payments=[], counts=[0, 0, 0], demos={}),
        )

    async def test_same_balance_request_recovers_after_panel_configuration(self):
        key = "f01-missing-then-configured"
        self.assertEqual((await self.checkout(None, 1, key)).status_code, 409)
        await self.panel()
        response = await self.checkout(None, 1, key)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.checkout(None, 1, key)).json(), response.json())
        self.assertEqual(len(self.api.creates), 1)
        await self.assert_real()

    async def test_concurrent_balance_retry_after_missing_panel_is_one_real_purchase(
        self,
    ):
        key = "f01-concurrent-recovery"
        first = await asyncio.gather(*(self.checkout(None, 1, key) for _ in range(2)))
        self.assertEqual([r.status_code for r in first], [409, 409])
        await self.panel()
        retry = await asyncio.gather(*(self.checkout(None, 1, key) for _ in range(2)))
        self.assertEqual([r.status_code for r in retry], [200, 200])
        self.assertEqual(retry[0].json(), retry[1].json())
        self.assertEqual(len(self.api.creates), 1)
        await self.assert_real()

    async def test_paid_order_recovers_same_payment_after_missing_panel(self):
        order = await self.paid_order(0, 1)
        with self.assertRaises(ValueError):
            await self.receipt(order)
        await self.assert_pending(order)
        await self.panel()
        self.assertTrue(await self.receipt(order))
        self.assertTrue(await self.receipt(order))
        self.assertEqual(len(self.api.creates), 1)
        await self.assert_real(paid_order=order, balance=100_000)

    async def test_paid_order_unavailable_panel_then_concurrent_recovery(self):
        pid = await self.panel(health="offline")
        order = await self.paid_order(0, 1)
        with self.assertRaises(ValueError):
            await self.receipt(order)
        await self.assert_pending(order)
        async with self.db.sessions.begin() as session:
            (await session.get(RemnawaveServer, pid)).health = "online"
        results = await asyncio.gather(self.receipt(order), self.receipt(order))
        self.assertEqual(results, [True, True])
        self.assertEqual(len(self.api.creates), 1)
        await self.assert_real(paid_order=order, balance=100_000)

    async def test_paid_order_rejected_creation_stays_pending_and_recovers(self):
        await self.panel()
        order = await self.paid_order(0, 1)
        self.api.creation_error = 401
        with self.assertRaises(ValueError):
            await self.receipt(order)
        await self.assert_pending(order)
        self.api.creation_error = None
        self.assertTrue(await self.receipt(order))
        await self.assert_real(paid_order=order, balance=100_000)

    async def test_paid_order_lost_remote_response_recovers_after_restart(self):
        await self.panel()
        order = await self.paid_order(0, 1)
        self.api.lose_response = True
        with self.assertRaises(ValueError):
            await self.receipt(order)
        await self.assert_pending(order)
        self.assertEqual(len(self.api.creates), 1)
        self.assertEqual(len(self.api.remote), 1)
        self.api = Panel(self.api.remote)
        await self.db.engine.dispose()
        self.assertTrue(await self.receipt(order))
        self.assertEqual(self.api.creates, [])
        await self.assert_real(paid_order=order, balance=100_000)

    async def test_paid_order_local_commit_failure_preserves_receipt_and_recovers(self):
        await self.panel()
        order = await self.paid_order(0, 1)
        original = vless_provision.provision

        async def provision(repo, *args, **kwargs):
            result = await original(repo, *args, **kwargs)
            repo.session.info["fail_fixture_commit"] = True
            return result

        def fail_commit(session):
            if session.info.pop("fail_fixture_commit", False):
                raise SQLAlchemyError("Injected checkout commit failure")

        event.listen(Session, "before_commit", fail_commit)
        try:
            with patch.object(vless_provision, "provision", provision):
                with self.assertRaises(SQLAlchemyError):
                    await self.receipt(order)
        finally:
            event.remove(Session, "before_commit", fail_commit)
        await self.assert_pending(order)
        self.assertTrue(await self.receipt(order))
        self.assertEqual(len(self.api.creates), 1)
        await self.assert_real(paid_order=order, balance=100_000)

    async def test_reconciler_preserves_paid_order_until_real_provisioning(self):
        order = await self.paid_order(0, 1)
        provider = SimpleNamespace(
            get=AsyncMock(return_value=order[1]), create=AsyncMock()
        )
        with patch("app.services.topups.YooKassa", return_value=provider):
            await reconcile(self.db, self.settings)
            await self.assert_pending(order)
            await self.panel()
            await reconcile(self.db, self.settings)
            await reconcile(self.db, self.settings)
        provider.create.assert_not_awaited()
        self.assertEqual(len(self.api.creates), 1)
        await self.assert_real(paid_order=order, balance=100_000)

    async def test_status_poll_distinguishes_paid_receipt_from_activated_subscription(
        self,
    ):
        order = await self.paid_order(0, 1)
        with (
            patch("app.services.topups.YooKassa.get", return_value=order[1]),
            patch("app.services.topups.YooKassa.create") as create,
        ):
            pending = await self.client.get(f"/api/web/payments/{order[0]}")
            self.assertEqual(pending.status_code, 200, pending.text)
            data = pending.json()
            self.assertTrue(data["confirmed"])  # Bank receipt, not activation.
            self.assertEqual(data["status"], "order_pending")
            self.assertIsNone(data["key_id"])
            self.assertIsNone(data["subscription_id"])
            await self.assert_pending(order)
            await self.panel()
            ready = await self.client.get(f"/api/web/payments/{order[0]}")
        create.assert_not_awaited()
        self.assertEqual(ready.json()["status"], "order_paid")
        self.assertIsNotNone(ready.json()["key_id"])
        await self.assert_real(paid_order=order, balance=100_000)

    async def test_paid_order_retains_balance_hold_without_debiting_twice(self):
        order = await self.paid_order(0, 1)
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            payment = await session.get(Payment, order[0])
            payment.amount = 5_000
            data = json.loads(await repo.setting(f"order:{payment.id}"))
            data.update(amount=5_000, total=10_000, balance_applied=5_000)
            await repo.set_setting(f"order:{payment.id}", json.dumps(data))
            session.add(
                BalanceTransaction(
                    user_id=self.uid,
                    type="checkout_hold",
                    amount=-5_000,
                    description="Fixture reserve",
                )
            )
        order[1]["amount"]["value"] = "50.00"
        with self.assertRaises(ValueError):
            await self.receipt(order)
        await self.assert_pending(order, balance=95_000)
        await self.panel()
        self.assertTrue(await self.receipt(order))
        self.assertTrue(await self.receipt(order))
        await self.assert_real(paid_order=order, balance=95_000)

    async def test_production_renewal_of_historical_demo_does_not_charge(self):
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            sub = await repo.add(
                Subscription(user_id=self.uid, kind="paid", expires_at=NOW)
            )
            key = await repo.add(
                AccessKey(user_id=self.uid, name="Historical demo", expires_at=NOW)
            )
            await repo.set_setting(f"subscription_key:{sub.id}", str(key.id))
            sid = sub.id
        response = await self.checkout(sid, 1, "f01-legacy-demo-renewal")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual((await self.state())["balance"], 100_000)
        self.assertEqual((await self.state())["payments"], [])

    async def test_daily_autorenew_of_historical_demo_does_not_charge(self):
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            sub = await repo.add(
                Subscription(
                    user_id=self.uid, kind="paid", expires_at=NOW - timedelta(days=1)
                )
            )
            key = await repo.add(
                AccessKey(
                    user_id=self.uid, name="Historical demo", expires_at=sub.expires_at
                )
            )
            await repo.set_setting(f"subscription_key:{sub.id}", str(key.id))
            await repo.set_setting(f"auto_renew:{sub.id}", "1")
            await repo.set_setting(f"auto_renew_cycle:{sub.id}", "daily")
        async with self.db.sessions.begin() as session:
            await prepare_autorenew(SQLRepository(session), self.settings)
        self.assertEqual((await self.state())["balance"], 100_000)
        self.assertEqual((await self.state())["payments"], [])

    async def test_demo_provider_fallback_cannot_bypass_production_policy(self):
        with patch("app.services.vless_provision.ensure", return_value=None):
            response = await self.checkout(None, 1, "f01-defense-in-depth")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual((await self.state())["counts"], [0, 0, 0])
        self.assertEqual((await self.state())["payments"], [])
        self.assertEqual((await self.state())["balance"], 100_000)

    async def test_demo_checkout_requires_explicit_isolated_session_opt_in(self):
        self.db.sessions.configure(info={"isolated_demo_provisioning": True})
        response = await self.checkout(None, 1, "f01-explicit-test-demo")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.state())["counts"], [1, 1, 0])
        self.assertTrue((await self.state())["demos"])

    async def test_production_never_serves_or_regenerates_historical_demo(self):
        self.db.sessions.configure(info={"isolated_demo_provisioning": True})
        response = await self.checkout(None, 1, "f01-old-demo")
        kid, sid = response.json()["key_id"], response.json()["subscription_id"]
        self.db.sessions.configure(info={})
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            key = await session.get(AccessKey, kid)
            with self.assertRaises(ValueError):
                await create_demo_file(repo, key)
            with self.assertRaises(ValueError):
                await key_screen(repo, self.settings, self.user, kid, sid)
            with self.assertRaises(HTTPException) as error:
                await connection_data(repo, self.settings, self.uid, kid)
            self.assertEqual(error.exception.status_code, 404)

    async def test_production_trial_still_upgrades_placeholder_to_real_vless(self):
        await self.panel()
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            (await session.get(User, self.uid)).trial_used = False
            await session.flush()
            sub = await SubscriptionService(repo, 1).activate_trial(self.uid)
            self.assertIsNotNone(sub)
            self.assertEqual(await repo.settings_prefix("demo_file:"), {})
            credential = await vless_provision.ensure(
                repo,
                self.settings,
                sub,
                await session.get(User, self.uid),
                operation_key=f"trial:{self.uid}",
            )
            self.assertIsNotNone(credential)
            key = await session.get(AccessKey, credential.access_key_id)
            self.assertEqual(key.mode, "vless")
            self.assertFalse(await repo.setting(f"demo_file:{key.id}"))
        state = await self.state()
        self.assertEqual(state["counts"], [1, 1, 1])
        self.assertEqual(state["balance"], 100_000)
        self.assertEqual(state["payments"], [])
