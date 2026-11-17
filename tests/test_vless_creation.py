"""PostgreSQL VLESS creation journal; all RemnaWave mutations are fixture-only."""

import asyncio
import copy
import json
import os
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from test_billing_concurrency import NOW, BillingConcurrency

from app.database.models import (
    AccessKey,
    Payment,
    RemnawaveServer,
    Subscription,
    User,
    VlessCreation,
    VlessCredential,
    VlessReservation,
    now,
)
from app.database.repositories import SQLRepository
from app.database.vless_creation_migration import upgrade
from app.services import vless_provision
from app.services.external_contacts import create as external_customer
from app.services.remnawave import RemnawaveApiClient, RemnawaveApiError
from app.services.stars import StarService
from app.services.vless_creation import confirmed, recover, validate_limits


class ProvisioningValidation(unittest.TestCase):
    def setUp(self):
        self.body = dict(
            username="fo_validation_fixture",
            status="ACTIVE",
            expireAt=(now() + timedelta(days=30)).isoformat(),
            hwidDeviceLimit=1,
            trafficLimitBytes=0,
            trafficLimitStrategy="NO_RESET",
            activeInternalSquads=["fixture-squad"],
            description="FO provisioning:fixture",
        )
        self.remote = {
            **self.body,
            "id": 101,
            "shortUuid": "fixture-short",
            "subscriptionUrl": "https://sub.example.test/fixture",
        }

    def test_boolean_device_limit_is_not_confirmed(self):
        with self.assertRaises(ValueError):
            confirmed({"response": {**self.remote, "hwidDeviceLimit": True}}, self.body)

    def test_boolean_zero_traffic_limit_is_not_confirmed(self):
        with self.assertRaises(ValueError):
            confirmed(
                {"response": {**self.remote, "trafficLimitBytes": False}}, self.body
            )

    def test_literal_whitespace_in_subscription_url_is_not_confirmed(self):
        with self.assertRaises(ValueError):
            confirmed(
                {
                    "response": {
                        **self.remote,
                        "subscriptionUrl": "https://sub.example.test/fixture with space",
                    }
                },
                self.body,
            )

    def test_non_integer_or_missing_limits_are_not_confirmed(self):
        for field in ("hwidDeviceLimit", "trafficLimitBytes"):
            for value in (None, "0", "1", 0.0, 1.0, -1, {}, []):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValueError),
                ):
                    confirmed({"response": {**self.remote, field: value}}, self.body)
            remote = {**self.remote}
            remote.pop(field)
            with self.assertRaises(ValueError):
                confirmed({"response": remote}, self.body)

    def test_positive_and_explicit_unlimited_numeric_limits_are_preserved(self):
        for devices, traffic in ((0, 0), (1, 0), (2, 1024**3), (2**31 - 1, 2**63 - 1)):
            with self.subTest(devices=devices, traffic=traffic):
                limits = dict(hwidDeviceLimit=devices, trafficLimitBytes=traffic)
                result = confirmed(
                    {"response": {**self.remote, **limits}}, {**self.body, **limits}
                )
                self.assertEqual(
                    result["subscription_url"], self.remote["subscriptionUrl"]
                )
                validate_limits(devices, traffic)

    def test_raw_whitespace_controls_and_malformed_urls_are_rejected(self):
        urls = [
            None,
            True,
            123,
            [],
            {},
            "",
            "relative",
            "javascript:alert(1)",
            "https:///missing-host",
            "https://user:password@example.test/fixture",
            "https://example.test:bad/fixture",
            "https://example.test:65536/fixture",
            "https://[invalid/fixture",
            "https://example.test\\/fixture",
        ]
        for char in (" ", "\t", "\n", "\r", "\x00", "\x1f", "\x7f", "\u00a0"):
            urls.extend(
                (
                    char + self.remote["subscriptionUrl"],
                    self.remote["subscriptionUrl"] + char,
                    "https://sub.example.test/fi" + char + "xture",
                )
            )
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                confirmed(
                    {"response": {**self.remote, "subscriptionUrl": url}}, self.body
                )

    def test_valid_urls_are_not_rewritten_or_overrestricted(self):
        for url in (
            "http://sub.example.test/fixture",
            "https://sub.example.test:8443/key%20name?token=fixture#connect",
            "https://[::1]:443/fixture",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    confirmed(
                        {"response": {**self.remote, "subscriptionUrl": url}}, self.body
                    )["subscription_url"],
                    url,
                )


class ProvisioningPreflight(unittest.IsolatedAsyncioTestCase):
    async def test_boolean_limits_in_admin_payloads_return_422_before_any_handler_work(
        self,
    ):
        from fastapi import FastAPI
        from app.web.catalog import catalog_router
        from app.web.admin_dashboard import admin_dashboard_router

        current_user = AsyncMock(side_effect=AssertionError("Handler must not start"))
        app = FastAPI()
        app.include_router(catalog_router(None, None, current_user))
        app.include_router(admin_dashboard_router(None, None, current_user))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://example.test"
        ) as client:
            catalog = dict(
                kind="vpn",
                name="Fixture",
                price="100",
                months=1,
                devices=1,
                traffic_gb=0,
                confirmed=True,
                request_id="validation-fixture",
            )
            for field, value in (
                ("devices", True),
                ("devices", False),
                ("traffic_gb", True),
                ("traffic_gb", False),
            ):
                with self.subTest(field=field, value=value):
                    response = await client.post(
                        "/api/web/admin/catalog", json={**catalog, field: value}
                    )
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertIn(field, str(response.json()["detail"]))
            response = await client.post(
                "/api/web/admin/keys/grant",
                json=dict(
                    user_id=1, device_limit=True, request_id="validation-fixture"
                ),
            )
            self.assertEqual(response.status_code, 422, response.text)
        current_user.assert_not_awaited()

    async def test_invalid_create_request_is_rejected_before_any_database_or_api_call(
        self,
    ):
        for devices, traffic in (
            (True, 0),
            (1, False),
            (1, None),
            (-1, 0),
            (1, 2**63),
            (2**31, 0),
        ):
            with self.subTest(devices=devices, traffic=traffic):
                # None dependencies make any DB/provider access a test failure.
                with self.assertRaisesRegex(ValueError, "лимит"):
                    await recover(
                        None,
                        None,
                        "fixture",
                        SimpleNamespace(device_limit=devices),
                        None,
                        None,
                        traffic,
                    )

    async def test_invalid_paid_limits_fail_before_fulfillment_mutations(self):
        from app.services.billing import BillingService

        repo = SimpleNamespace(
            setting=AsyncMock(return_value="0"),
            session=None,
            add=AsyncMock(),
            set_setting=AsyncMock(),
        )
        tariff = SimpleNamespace(id=1, device_limit=1)
        for terms in (
            {"devices": True, "traffic_bytes": 0},
            {"devices": 1, "traffic_bytes": False},
            {"devices": 1, "traffic_bytes": "0"},
            {"devices": 1, "traffic_bytes": None},
        ):
            with (
                self.subTest(terms=terms),
                self.assertRaisesRegex(ValueError, "лимит|условия"),
            ):
                await BillingService(repo, None).fulfill(None, tariff, terms=terms)
        repo.add.assert_not_awaited()
        repo.set_setting.assert_not_awaited()


class Panel:
    def __init__(self, remote=None):
        self.remote = remote if remote is not None else {}
        self.creates = []
        self.lookups = []
        self.creation_error = None
        self.lose_response = False
        self.lookup_error = False
        self.malformed_response = False
        self.response_overrides = {}
        self.entered = self.release = None

    async def user_by_username(self, username):
        self.lookups.append(username)
        if self.lookup_error:
            raise RemnawaveApiError("Injected lookup outage", 503)
        if username not in self.remote:
            raise RemnawaveApiError("Not found", 404, error_code="A025")
        return {
            "response": {
                **copy.deepcopy(self.remote[username]),
                **self.response_overrides,
            }
        }

    async def create_user(self, body):
        self.creates.append(copy.deepcopy(body))
        if self.creation_error:
            raise RemnawaveApiError("Injected creation rejection", self.creation_error)
        if body["username"] in self.remote:
            raise RemnawaveApiError("Duplicate username", 409)
        self.remote[body["username"]] = {
            **copy.deepcopy(body),
            "activeInternalSquads": [{"uuid": s} for s in body["activeInternalSquads"]],
            "id": 100 + len(self.remote),
            "shortUuid": "fixture-short",
            "subscriptionUrl": "https://sub.example.test/fixture",
        }
        if self.entered:
            self.entered.set()
            await asyncio.wait_for(self.release.wait(), 10)
        if self.lose_response:
            self.lookup_error = True
            raise RemnawaveApiError("Injected timeout after remote commit")
        if self.malformed_response:
            return {"response": {}}
        return {
            "response": {
                **copy.deepcopy(self.remote[body["username"]]),
                **self.response_overrides,
            }
        }


@unittest.skipUnless(os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL")
class VlessCreationConsistency(unittest.IsolatedAsyncioTestCase):
    checkout = BillingConcurrency.checkout
    paid_order = BillingConcurrency.paid_order
    receipt = BillingConcurrency.receipt

    async def asyncSetUp(self):
        await BillingConcurrency.asyncSetUp(self)
        self.api = Panel()
        self.api_patch = patch(
            "app.services.remnawave.RemnawaveService.api",
            side_effect=lambda *_: self.api,
        )
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)
        async with self.db.sessions.begin() as session:
            panel = RemnawaveServer(
                name="Fixture",
                country="TEST",
                api_url="https://panel.example.test",
                api_token="fixture-only",
                internal_squad_uuid="fixture-squad",
                health="online",
                max_users=10,
            )
            session.add(panel)
            await session.flush()
            self.pid = panel.id

    async def journal(self):
        async with self.db.sessions() as session:
            rows = list(
                await session.scalars(
                    select(VlessCreation).order_by(VlessCreation.created_at)
                )
            )
            return rows

    async def counts(self):
        async with self.db.sessions() as session:
            counts = [
                await session.scalar(select(func.count()).select_from(m))
                for m in (Payment, Subscription, AccessKey, VlessCredential)
            ]
            count = (await session.get(RemnawaveServer, self.pid)).users_total
            return (*counts, count)

    async def saved_subscription(self):
        async with self.db.sessions.begin() as session:
            sub = Subscription(
                user_id=self.uid, kind="paid", expires_at=now() + timedelta(days=30)
            )
            session.add(sub)
            await session.flush()
            return sub.id

    async def issue(self, sid):
        async with self.db.sessions.begin() as session:
            credential = await vless_provision.ensure(
                SQLRepository(session),
                self.settings,
                await session.get(Subscription, sid),
                await session.get(User, self.uid),
            )
            return credential.id

    async def assert_purchase(self):
        self.assertEqual(await self.counts(), (1, 1, 1, 1, 1))
        self.assertEqual(len(self.api.remote), 1)
        async with self.db.sessions() as session:
            self.assertEqual(await SQLRepository(session).balance(self.uid), 90_000)
            sub = await session.scalar(select(Subscription))
            credential = await session.scalar(select(VlessCredential))
            operation = await session.scalar(select(VlessCreation))
            self.assertEqual(operation.state, "associated")
            self.assertEqual(operation.subscription_id, sub.id)
            self.assertEqual(operation.credential_id, credential.id)
            self.assertEqual(sub.expires_at, credential.expires_at)
            self.assertEqual(credential.remote_user_uuid, "100")
            self.assertEqual(
                json.loads(operation.request)["expireAt"], sub.expires_at.isoformat()
            )

    async def test_purchase_timeout_retries_same_remote_after_subscription_id_rollback(
        self,
    ):
        self.api.lose_response = True
        first = await self.checkout(None, 1, "vless-response-lost")
        self.assertEqual(first.status_code, 409)
        self.assertEqual(await self.counts(), (0, 0, 0, 0, 0))
        operation = (await self.journal())[0]
        self.assertEqual(operation.state, "outcome_unknown")
        self.assertIsNone(operation.credential_id)
        self.api.lose_response = self.api.lookup_error = False
        with patch("app.services.billing.now", return_value=NOW + timedelta(hours=3)):
            retry = await self.checkout(None, 1, "vless-response-lost")
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertGreater(retry.json()["subscription_id"], 1)
        self.assertEqual(len(self.api.creates), 1)
        self.assertEqual(len(set(self.api.lookups)), 1)
        await self.assert_purchase()

    async def test_uncertain_creation_cannot_be_abandoned_by_checkout_cancellation(
        self,
    ):
        key = "vless-uncertain-cancel"
        self.api.lose_response = True
        self.assertEqual((await self.checkout(None, 1, key)).status_code, 409)
        response = await self.client.post(
            "/api/web/checkout/cancel",
            json=dict(months=1, method="balance", idempotency_key=key),
        )
        self.assertEqual(response.status_code, 409, response.text)
        operation = await self.client.get(
            "/api/web/checkout", params={"idempotency_key": key}
        )
        self.assertIsNone(operation.json()["result"])
        self.api.lose_response = self.api.lookup_error = False
        self.assertEqual((await self.checkout(None, 1, key)).status_code, 200)
        self.assertEqual(len(self.api.creates), 1)
        await self.assert_purchase()

    async def test_uncertain_owner_grant_cannot_be_canceled_as_unstarted(self):
        key = "vless-uncertain-owner"
        with patch.object(type(self.settings), "is_owner", return_value=True):
            self.api.lose_response = True
            self.assertEqual((await self.checkout(None, 1, key)).status_code, 409)
            response = await self.client.post(
                "/api/web/checkout/cancel",
                json=dict(months=1, method="balance", idempotency_key=key),
            )
            self.assertEqual(response.status_code, 409, response.text)
            self.api.lose_response = self.api.lookup_error = False
            self.assertEqual((await self.checkout(None, 1, key)).status_code, 200)
        self.assertEqual(len(self.api.creates), 1)
        self.assertEqual(await self.counts(), (0, 1, 1, 1, 1))

    async def test_confirmed_rejected_creation_can_be_canceled(self):
        key = "vless-rejected-cancel"
        self.api.creation_error = 401
        self.assertEqual((await self.checkout(None, 1, key)).status_code, 409)
        response = await self.client.post(
            "/api/web/checkout/cancel",
            json=dict(months=1, method="balance", idempotency_key=key),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["status"], "canceled")
        self.api.creation_error = None
        retry = await self.checkout(None, 1, key)
        self.assertEqual(retry.json()["status"], "canceled")
        self.assertEqual(await self.counts(), (0, 0, 0, 0, 0))
        self.assertEqual(len(self.api.remote), 0)

    async def test_creation_journal_is_committed_before_external_call(self):
        self.api.entered, self.api.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.checkout(None, 1, "vless-before-dispatch"))
        try:
            await asyncio.wait_for(self.api.entered.wait(), 10)
            operations = await self.journal()
            self.assertEqual(len(operations), 1)
            self.assertEqual(operations[0].state, "outcome_unknown")
            self.assertEqual(
                json.loads(operations[0].request)["username"],
                self.api.creates[0]["username"],
            )
            self.assertEqual(await self.counts(), (0, 0, 0, 0, 0))
        finally:
            self.api.release.set()
        self.assertEqual((await task).status_code, 200)
        await self.assert_purchase()

    async def test_journal_does_not_require_spare_request_pool_connections(self):
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
        response = await asyncio.wait_for(
            self.checkout(None, 1, "vless-single-pool"), 15
        )
        self.assertEqual(response.status_code, 200, response.text)
        await self.assert_purchase()

    async def test_paid_card_receipt_recovers_creation_without_duplicate_subscription(
        self,
    ):
        order = await self.paid_order(0, 1)
        self.api.lose_response = True
        with self.assertRaises(ValueError):
            await self.receipt(order)
        self.assertEqual(await self.counts(), (1, 0, 0, 0, 0))
        self.api.lose_response = self.api.lookup_error = False
        self.assertTrue(await self.receipt(order))
        self.assertTrue(await self.receipt(order))
        self.assertEqual(await self.counts(), (1, 1, 1, 1, 1))
        self.assertEqual(len(self.api.creates), 1)
        async with self.db.sessions() as session:
            self.assertEqual(await SQLRepository(session).balance(self.uid), 100_000)

    async def test_stars_receipt_recovers_creation_with_persisted_payment_identity(
        self,
    ):
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            await repo.set_setting("payment_provider:stars", "1")
            await repo.set_setting("stars_price:1", "75")
            invoice = await StarService(repo, self.settings).create(self.user, 1)
        paid = SimpleNamespace(
            invoice_payload=invoice["payload"],
            currency="XTR",
            total_amount=75,
            telegram_payment_charge_id="fixture-charge",
        )

        async def complete():
            async with self.db.sessions.begin() as session:
                return await StarService(
                    SQLRepository(session), self.settings
                ).complete(paid, self.user)

        self.api.lose_response = True
        with self.assertRaises(ValueError):
            await complete()
        self.api.lose_response = self.api.lookup_error = False
        with patch.object(StarService, "success_screen", new_callable=AsyncMock):
            await complete()
            await complete()
        self.assertEqual(await self.counts(), (1, 1, 1, 1, 1))
        self.assertEqual(len(self.api.creates), 1)

    async def test_late_creation_conflict_recovers_by_lookup(self):
        self.api.lose_response = True
        self.assertEqual(
            (await self.checkout(None, 1, "vless-late-conflict")).status_code, 409
        )
        self.api.lose_response = self.api.lookup_error = False
        original, stale = self.api.user_by_username, True

        async def lookup(username):
            nonlocal stale
            if stale:
                stale = False
                raise RemnawaveApiError(
                    "Injected stale read before late commit", 404, error_code="A025"
                )
            return await original(username)

        with patch.object(self.api, "user_by_username", side_effect=lookup):
            response = await self.checkout(None, 1, "vless-late-conflict")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.api.creates), 2)
        await self.assert_purchase()

    async def test_successful_remote_create_followed_by_failed_local_commit(self):
        original = vless_provision.provision
        armed = True

        async def issue(repo, *args, **kwargs):
            nonlocal armed
            result = await original(repo, *args, **kwargs)
            if armed:
                armed = False
                repo.session.info["fail_fixture_commit"] = True
            return result

        def fail(session):
            if session.info.pop("fail_fixture_commit", False):
                raise SQLAlchemyError("Injected local commit failure")

        event.listen(Session, "before_commit", fail)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=self.client._transport.app, raise_app_exceptions=False
            ),
            base_url=self.settings.web_base_url,
        )
        try:
            with patch.object(vless_provision, "provision", issue):
                first = await client.post(
                    "/api/web/checkout",
                    json=dict(
                        months=1, method="balance", idempotency_key="vless-local-commit"
                    ),
                )
            self.assertEqual(first.status_code, 500)
        finally:
            event.remove(Session, "before_commit", fail)
            await client.aclose()
        self.assertEqual(await self.counts(), (0, 0, 0, 0, 1))
        self.assertEqual((await self.journal())[0].state, "remote_confirmed")
        cancellation = await self.client.post(
            "/api/web/checkout/cancel",
            json=dict(months=1, method="balance", idempotency_key="vless-local-commit"),
        )
        self.assertEqual(cancellation.status_code, 409, cancellation.text)
        retry = await self.checkout(None, 1, "vless-local-commit")
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(len(self.api.creates), 1)
        await self.assert_purchase()

    async def test_failed_journal_confirmation_commit_recovers_by_remote_lookup(self):
        armed = True

        def fail(session):
            nonlocal armed
            if armed and any(
                isinstance(row, VlessCreation) and row.state == "remote_confirmed"
                for row in session.dirty
            ):
                armed = False
                raise SQLAlchemyError("Injected journal confirmation commit failure")

        event.listen(Session, "before_commit", fail)
        try:
            first = await self.checkout(None, 1, "vless-journal-commit")
            self.assertEqual(first.status_code, 409)
        finally:
            event.remove(Session, "before_commit", fail)
        self.assertFalse(armed)
        self.assertEqual((await self.journal())[0].state, "outcome_unknown")
        self.assertEqual(await self.counts(), (0, 0, 0, 0, 0))
        retry = await self.checkout(None, 1, "vless-journal-commit")
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(len(self.api.creates), 1)
        await self.assert_purchase()

    async def test_restart_recovers_without_process_memory(self):
        self.api.lose_response = True
        self.assertEqual(
            (await self.checkout(None, 1, "vless-process-restart")).status_code, 409
        )
        # Replace the adapter and connection pool; only the DB journal and the
        # separately hosted provider state survive this simulated restart.
        self.api = Panel(self.api.remote)
        await self.db.engine.dispose()
        response = await self.checkout(None, 1, "vless-process-restart")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.api.creates, [])
        await self.assert_purchase()

    async def test_external_customer_and_subscription_rollback_keep_same_grant_identity(
        self,
    ):
        first_user = None
        with self.assertRaises(RuntimeError):
            async with self.db.sessions.begin() as session:
                repo = SQLRepository(session)
                customer = await external_customer(repo, self.settings, "+79990000000")
                first_user = customer.id
                sub = await repo.add(
                    Subscription(
                        user_id=customer.id,
                        kind="admin",
                        expires_at=now() + timedelta(days=30),
                    )
                )
                await vless_provision.ensure(
                    repo,
                    self.settings,
                    sub,
                    customer,
                    operation_key="web-grant:fixture-intent",
                )
                raise RuntimeError("Injected customer/grant rollback")
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            customer = await external_customer(repo, self.settings, "+79990000000")
            self.assertNotEqual(customer.id, first_user)
            sub = await repo.add(
                Subscription(
                    user_id=customer.id,
                    kind="admin",
                    expires_at=now() + timedelta(days=30),
                )
            )
            credential = await vless_provision.ensure(
                repo,
                self.settings,
                sub,
                customer,
                operation_key="web-grant:fixture-intent",
            )
            self.assertEqual(credential.user_id, customer.id)
        self.assertEqual(len(self.api.remote), 1)
        self.assertEqual(len(self.api.creates), 1)
        self.assertEqual(await self.counts(), (0, 1, 1, 1, 1))

    async def test_persisted_subscription_timeout_recovers_local_association(self):
        sid = await self.saved_subscription()
        self.api.lose_response = True
        with self.assertRaises(ValueError):
            await self.issue(sid)
        self.api.lose_response = self.api.lookup_error = False
        await self.issue(sid)
        self.assertEqual(await self.counts(), (0, 1, 1, 1, 1))
        self.assertEqual(len(self.api.creates), 1)

    async def test_concurrent_same_subscription_creates_and_associates_once(self):
        sid = await self.saved_subscription()
        self.api.entered, self.api.release = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(self.issue(sid))
        try:
            await asyncio.wait_for(self.api.entered.wait(), 10)
            second = asyncio.create_task(self.issue(sid))
            await asyncio.sleep(0.1)
            self.assertFalse(second.done())
        finally:
            self.api.release.set()
        replies = await asyncio.wait_for(asyncio.gather(first, second), 20)
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(await self.counts(), (0, 1, 1, 1, 1))
        self.assertEqual(len(self.api.creates), 1)

    async def test_existing_subscription_uses_same_creation_across_request_keys(self):
        sid = await self.saved_subscription()

        async def issue(key):
            async with self.db.sessions.begin() as session:
                c = await vless_provision.ensure(
                    SQLRepository(session),
                    self.settings,
                    await session.get(Subscription, sid),
                    self.user,
                    operation_key=key,
                )
                return c.id

        results = await asyncio.wait_for(
            asyncio.gather(issue("request-one"), issue("request-two")), 20
        )
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.api.creates), 1)
        self.assertEqual(await self.counts(), (0, 1, 1, 1, 1))

    async def test_concurrent_same_checkout_has_one_remote_key_and_charge(self):
        responses = await asyncio.wait_for(
            asyncio.gather(
                self.checkout(None, 1, "vless-concurrent-checkout"),
                self.checkout(None, 1, "vless-concurrent-checkout"),
            ),
            20,
        )
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(responses[0].json(), responses[1].json())
        self.assertEqual(len(self.api.creates), 1)
        await self.assert_purchase()

    async def test_confirmed_rejection_is_distinct_from_unknown_failure(self):
        for status, state in ((401, "failed"), (503, "outcome_unknown")):
            with self.subTest(status=status):
                self.api.creation_error = status
                key = "vless-rejected-" + str(status)
                first = await self.checkout(None, 1, key)
                self.assertEqual(first.status_code, 409)
                self.assertEqual((await self.journal())[-1].state, state)
                self.assertEqual(await self.counts(), (0, 0, 0, 0, 0))
        self.assertEqual(len(self.api.remote), 0)

    async def test_lookup_failure_never_causes_blind_create_or_activation(self):
        self.api.lookup_error = True
        response = await self.checkout(None, 1, "vless-lookup-outage")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.api.creates, [])
        self.assertEqual(await self.counts(), (0, 0, 0, 0, 0))

    async def test_malformed_create_response_recovers_instead_of_creating_again(self):
        self.api.malformed_response = True
        response = await self.checkout(None, 1, "vless-malformed-answer")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(await self.counts(), (0, 0, 0, 0, 0))
        self.api.malformed_response = False
        response = await self.checkout(None, 1, "vless-malformed-answer")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.api.creates), 1)
        await self.assert_purchase()

    async def malformed_creation_recovery(self, override):
        self.api.response_overrides = override
        key = "validation-malformed-create"
        for _ in range(2):
            response = await self.checkout(None, 1, key)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("не подтверждён", response.json()["detail"])
            self.assertEqual(await self.counts(), (0, 0, 0, 0, 0))
            async with self.db.sessions() as session:
                self.assertEqual(
                    await SQLRepository(session).balance(self.uid), 100_000
                )
                operation = await session.scalar(select(VlessCreation))
                reservation = await session.get(VlessReservation, operation.id)
                self.assertEqual(operation.state, "outcome_unknown")
                self.assertIsNone(operation.result)
                self.assertIsNone(operation.credential_id)
                self.assertEqual(reservation.state, "reserved")
        self.assertEqual(len(self.api.creates), 1)
        self.api.response_overrides = {}
        first = await self.checkout(None, 1, key)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual((await self.checkout(None, 1, key)).json(), first.json())
        self.assertEqual(len(self.api.creates), 1)
        await self.assert_purchase()

    async def test_boolean_device_response_keeps_uncertain_operation_and_recovers(self):
        await self.malformed_creation_recovery({"hwidDeviceLimit": True})

    async def test_boolean_zero_traffic_response_keeps_uncertain_operation_and_recovers(
        self,
    ):
        await self.malformed_creation_recovery({"trafficLimitBytes": False})

    async def test_whitespace_url_response_keeps_uncertain_operation_and_recovers(self):
        await self.malformed_creation_recovery(
            {"subscriptionUrl": "https://sub.example.test/bad url"}
        )

    async def test_paid_bank_order_with_malformed_response_keeps_original_payment(self):
        order = await self.paid_order(0, 1)
        self.api.response_overrides = {"trafficLimitBytes": False}
        with self.assertRaises(ValueError):
            await self.receipt(order)
        async with self.db.sessions() as session:
            payment = await session.get(Payment, order[0])
            self.assertEqual(payment.status, "order_pending")
            self.assertEqual(payment.provider_id, order[1]["id"])
            self.assertIsNone(payment.subscription_id)
            self.assertEqual(await SQLRepository(session).balance(self.uid), 100_000)
        self.assertEqual(await self.counts(), (1, 0, 0, 0, 0))
        self.api.response_overrides = {}
        await self.receipt(order)
        await self.receipt(order)
        self.assertEqual(len(self.api.creates), 1)
        self.assertEqual(await self.counts(), (1, 1, 1, 1, 1))

    async def test_wrong_remote_owner_or_parameters_are_never_adopted(self):
        self.api.lose_response = True
        self.assertEqual(
            (await self.checkout(None, 1, "vless-wrong-remote")).status_code, 409
        )
        self.api.lose_response = self.api.lookup_error = False
        remote = next(iter(self.api.remote.values()))
        for field, value in (
            ("description", "Another customer"),
            ("hwidDeviceLimit", 90),
            ("status", "DISABLED"),
        ):
            with self.subTest(field=field):
                saved = remote[field]
                remote[field] = value
                response = await self.checkout(None, 1, "vless-wrong-remote")
                self.assertEqual(response.status_code, 409)
                self.assertEqual(await self.counts(), (0, 0, 0, 0, 0))
                remote[field] = saved
        self.assertEqual(len(self.api.creates), 1)

    async def test_recovery_stays_on_original_panel_even_if_full_or_offline(self):
        self.api.lose_response = True
        self.assertEqual(
            (await self.checkout(None, 1, "vless-original-panel")).status_code, 409
        )
        self.api.lose_response = self.api.lookup_error = False
        async with self.db.sessions.begin() as session:
            panel = await session.get(RemnawaveServer, self.pid)
            panel.health, panel.users_total = "offline", panel.max_users
            session.add(
                RemnawaveServer(
                    name="Other",
                    country="TEST",
                    api_url="https://other.example.test",
                    api_token="fixture",
                    internal_squad_uuid="other-squad",
                    health="online",
                    priority=1,
                )
            )
        response = await self.checkout(None, 1, "vless-original-panel")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.api.creates), 1)
        async with self.db.sessions() as session:
            self.assertEqual(
                (await session.scalar(select(VlessCredential))).server_id, self.pid
            )

    async def test_previously_confirmed_but_missing_remote_is_not_recreated(self):
        sid = await self.saved_subscription()
        with self.assertRaises(RuntimeError):
            async with self.db.sessions.begin() as session:
                await vless_provision.ensure(
                    SQLRepository(session),
                    self.settings,
                    await session.get(Subscription, sid),
                    await session.get(User, self.uid),
                )
                raise RuntimeError("Injected rollback after successful provisioning")
        self.api.remote.clear()
        with self.assertRaises(ValueError):
            await self.issue(sid)
        self.assertEqual(len(self.api.creates), 1)
        self.assertEqual(await self.counts(), (0, 1, 0, 0, 1))

    async def test_existing_customer_key_is_returned_without_remote_changes(self):
        sid = await self.saved_subscription()
        cid = await self.issue(sid)
        self.api.creates.clear()
        self.api.lookups.clear()
        self.api.lookup_error = True
        self.assertEqual(await self.issue(sid), cid)
        self.assertEqual(self.api.creates, [])
        self.assertEqual(self.api.lookups, [])

    async def test_new_subscription_without_stable_order_is_rejected_before_api(self):
        with self.assertRaises(ValueError):
            async with self.db.sessions.begin() as session:
                sub = await SQLRepository(session).add(
                    Subscription(
                        user_id=self.uid, expires_at=now() + timedelta(days=30)
                    )
                )
                await vless_provision.ensure(
                    SQLRepository(session), self.settings, sub, self.user
                )
        self.assertEqual(self.api.creates, [])
        self.assertEqual(await self.journal(), [])

    async def test_additive_migration_is_repeatable_and_preserves_existing_rows(self):
        async with self.db.engine.begin() as connection:
            await connection.run_sync(VlessCreation.__table__.drop)
        await upgrade(self.db.engine)
        await upgrade(self.db.engine)
        sid = await self.saved_subscription()
        cid = await self.issue(sid)
        await upgrade(self.db.engine)
        self.assertEqual(await self.issue(sid), cid)
        async with self.db.sessions() as session:
            self.assertEqual((await session.get(User, self.uid)).freeman_id, "FO-1")


class InstalledApiContract(unittest.IsolatedAsyncioTestCase):
    async def test_username_lookup_uses_verified_get_endpoint_and_quotes_path(self):
        api = RemnawaveApiClient("https://panel.example.test", "fixture")
        api.request = AsyncMock(return_value={"response": {"id": 100}})
        await api.user_by_username("fo_abcd/path")
        api.request.assert_awaited_once_with("GET", "/users/by-username/fo_abcd%2Fpath")
