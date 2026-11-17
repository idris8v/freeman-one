"""Cabinet refactoring contracts and offline HTTP integration checks.

Uses temporary SQLite or the explicitly selected test PostgreSQL fixture. No
payment, Telegram, VPN or model provider receives a real request.
"""

import asyncio
import hashlib
import json
import os
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import func, select
from test_freeman_ai import Tests as DatabaseFixture

from app.config.settings import Settings
from app.database.models import (
    AccessKey,
    AppSetting,
    BalanceTransaction,
    RemnawaveServer,
    Subscription,
    Tariff,
    Ticket,
    TicketMessage,
    User,
    VlessCredential,
    VpnCredential,
    VpnServer,
    Withdrawal,
    now,
)
from app.database.repositories import SQLRepository
from app.services.freeman_ai.operations import app_for
from app.web.cabinet.context import WebContext
from app.web.routes import website_router
from app.web.security import SecurityMiddleware


class CabinetContract(unittest.TestCase):
    def test_existing_paths_payloads_cookies_and_responses_are_unchanged(self):
        settings = Settings(_env_file=None, bot_token="")
        router = website_router(SimpleNamespace(sessions=None), settings)
        direct = [r for r in router.routes if isinstance(r, APIRoute)]
        app = FastAPI()
        app.router.routes.extend(direct)
        schema = app.openapi()
        contract = {
            "routes": sorted(
                [
                    dict(
                        path=r.path,
                        methods=sorted(r.methods),
                        name=r.name,
                        status=r.status_code,
                        include_in_schema=r.include_in_schema,
                    )
                    for r in direct
                ],
                key=lambda r: (r["path"], r["methods"]),
            ),
            "paths": schema["paths"],
            "schemas": schema.get("components", {}).get("schemas", {}),
        }
        expected = json.loads(
            Path(__file__)
            .with_name("fixtures")
            .joinpath("cabinet-api.json")
            .read_text()
        )
        self.assertEqual(contract, expected)


class CabinetAPI(unittest.IsolatedAsyncioTestCase):
    asyncSetUpDatabase = DatabaseFixture.asyncSetUp
    asyncTearDownDatabase = DatabaseFixture.asyncTearDown

    async def asyncSetUp(self):
        await self.asyncSetUpDatabase()
        # Demo connection fixtures are restricted to this disposable test DB.
        self.db.sessions.configure(info={"isolated_demo_provisioning": True})
        self.settings = Settings(
            _env_file=None,
            encryption_key=self.settings.encryption_key,
            bot_token="",
            admin_ids="999",
            freeman_ai_enabled=False,
            web_base_url="https://example.test",
        )
        self.context = WebContext(self.db, self.settings)
        app = FastAPI()
        app.include_router(website_router(self.db, self.settings))
        app.add_middleware(SecurityMiddleware, web_base_url=self.settings.web_base_url)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=self.settings.web_base_url
        )
        self.storage = Path(self.tmp.name) / "attachments"
        self.patches = [
            patch(f"app.web.cabinet.{module}.SUPPORT_STORAGE", self.storage)
            for module in (
                "support_storage",
                "support",
                "support_media",
                "support_settings",
            )
        ]
        for item in self.patches:
            item.start()
        async with self.db.sessions.begin() as session:
            owner = User(telegram_id=999, first_name="Owner", freeman_id="FO-999")
            session.add(owner)
            await session.flush()
            self.owner_id = owner.id
        self.login(self.uid)

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()
        await self.asyncTearDownDatabase()

    def login(self, user_id, version=0):
        self.client.cookies.set(
            "freeman_session", self.context.session_token(user_id, version)
        )

    async def key(self, provider="demo", owner_id=None):
        uid = owner_id or self.uid
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            sub = await repo.add(
                Subscription(user_id=uid, expires_at=now() + timedelta(days=30))
            )
            key = await repo.add(
                AccessKey(user_id=uid, name="Test key", expires_at=sub.expires_at)
            )
            await repo.set_setting(f"subscription_key:{sub.id}", str(key.id))
            if provider == "demo":
                await repo.set_setting(f"demo_file:{key.id}", "fixture-config")
                value = f"https://example.invalid/freeman-demo/{key.id}/{hashlib.sha256(b'fixture-config').hexdigest()[:32]}"
            elif provider == "vless":
                server = await repo.add(
                    RemnawaveServer(
                        name="Fixture",
                        country="TEST",
                        api_url="https://vpn.invalid",
                        api_token="fixture",
                    )
                )
                value = "https://sub.invalid/fixture-key"
                await repo.add(
                    VlessCredential(
                        server_id=server.id,
                        user_id=uid,
                        subscription_id=sub.id,
                        access_key_id=key.id,
                        remote_user_uuid="fixture-uuid",
                        subscription_url=value,
                        expires_at=sub.expires_at,
                    )
                )
            elif provider == "vpn":
                server = await repo.add(
                    VpnServer(
                        name="Fixture",
                        country="TEST",
                        api_url="https://vpn.invalid",
                        api_key="fixture",
                    )
                )
                value = "[Interface]\nPrivateKey = fixture-only"
                await repo.add(
                    VpnCredential(
                        server_id=server.id,
                        user_id=uid,
                        subscription_id=sub.id,
                        access_key_id=key.id,
                        remote_client_id="fixture-client",
                        config=self.context.cipher().encrypt(value.encode()).decode(),
                        expires_at=sub.expires_at,
                    )
                )
            else:
                value = None
            return key.id, sub.id, value

    async def test_login_expiry_revocation_and_blocked_accounts(self):
        self.client.cookies.clear()
        self.assertEqual((await self.client.get("/api/web/me")).status_code, 401)
        self.client.cookies.set("freeman_session", "damaged-token")
        self.assertEqual((await self.client.get("/api/web/me")).status_code, 401)
        expired = (
            self.context.cipher()
            .encrypt(
                json.dumps({"user_id": self.uid, "expires": time.time() - 1}).encode()
            )
            .decode()
        )
        self.client.cookies.set("freeman_session", expired)
        self.assertEqual((await self.client.get("/api/web/me")).status_code, 401)
        self.login(self.uid, version=1)
        self.assertEqual((await self.client.get("/api/web/me")).status_code, 401)
        async with self.db.sessions.begin() as session:
            (await session.get(User, self.uid)).status = "blocked"
        self.login(self.uid)
        self.assertEqual((await self.client.get("/api/web/me")).status_code, 401)

    async def test_profile_content_documents_and_activity_record(self):
        response = await self.client.get("/api/web/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["profile"]["freeman_id_display"], "FO-1")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual((await self.client.get("/api/web/faq")).status_code, 200)
        self.assertEqual((await self.client.get("/documents")).status_code, 200)
        self.assertEqual((await self.client.get("/documents/999")).status_code, 404)
        async with self.db.sessions() as session:
            self.assertTrue(
                (
                    await session.get(AppSetting, f"client_seen:{self.uid}")
                ).value.isdigit()
            )

    async def test_connection_and_qr_have_same_value_for_each_provider(self):
        for provider in ("demo", "vless", "vpn"):
            with self.subTest(provider=provider):
                kid, _, value = await self.key(provider)
                link = await self.client.get(f"/api/web/keys/{kid}/connection")
                self.assertEqual(link.status_code, 200)
                self.assertEqual(link.json(), {"value": value, "kind": provider})
                with patch("qrcode.make") as encode:
                    encode.return_value.save.side_effect = lambda buffer, **_: (
                        buffer.write(b"fixture-png")
                    )
                    qr = await self.client.get(f"/api/web/keys/{kid}/qr")
                encode.assert_called_once_with(value)
                self.assertEqual(qr.content, b"fixture-png")
                self.assertEqual(qr.headers["content-type"], "image/png")

    async def test_foreign_deleted_and_unprovisioned_keys_stay_private(self):
        foreign, _, _ = await self.key(owner_id=self.other)
        deleted, _, _ = await self.key()
        empty, _, _ = await self.key("empty")
        async with self.db.sessions.begin() as session:
            (await session.get(AccessKey, deleted)).status = "deleted"
        for kid in (foreign, deleted, empty):
            for suffix in ("connection", "qr"):
                self.assertEqual(
                    (
                        await self.client.get(f"/api/web/keys/{kid}/{suffix}")
                    ).status_code,
                    404,
                )
        for kid in (foreign, deleted):
            self.assertEqual(
                (
                    await self.client.put(
                        f"/api/web/keys/{kid}/name", json={"name": "Renamed"}
                    )
                ).status_code,
                404,
            )
            self.assertEqual(
                (
                    await self.client.post(
                        f"/api/web/keys/{kid}/autorenew", json={"enabled": False}
                    )
                ).status_code,
                404,
            )

    async def test_rename_and_autorenew_update_the_linked_subscription(self):
        kid, sid, _ = await self.key()
        async with self.db.sessions.begin() as session:
            session.add(Tariff(months=1, price=1000, device_limit=1))
        renamed = await self.client.put(
            f"/api/web/keys/{kid}/name", json={"name": " My phone "}
        )
        self.assertEqual(renamed.json(), {"id": kid, "name": "My phone"})
        response = await self.client.post(
            f"/api/web/keys/{kid}/autorenew", json={"enabled": True, "months": 1}
        )
        self.assertEqual(response.status_code, 200)
        async with self.db.sessions() as session:
            repo = SQLRepository(session)
            self.assertEqual(await repo.setting(f"subscription_name:{sid}"), "My phone")
            self.assertEqual(await repo.setting(f"auto_renew:{sid}"), "1")
            self.assertEqual(await repo.setting(f"auto_source:{sid}"), "balance")

    async def test_support_retry_ownership_reply_and_delete_across_roles(self):
        payload = {
            "text": "Connection question",
            "request_id": "fixture-support-request",
        }
        first = await self.client.post("/api/web/support/messages", json=payload)
        self.assertEqual(first.status_code, 200)
        tid = first.json()["id"]
        retry = await self.client.post("/api/web/support/messages", json=payload)
        self.assertEqual(retry.json()["id"], tid)
        self.assertEqual(len(retry.json()["messages"]), 1)
        mismatch = await self.client.post(
            "/api/web/support/messages", json=payload | {"text": "Different request"}
        )
        self.assertEqual(mismatch.status_code, 409)
        self.login(self.other)
        self.assertEqual(
            (await self.client.get(f"/api/web/support?ticket_id={tid}")).status_code,
            404,
        )
        self.assertEqual(
            (await self.client.delete(f"/api/web/support/tickets/{tid}")).status_code,
            403,
        )
        self.login(self.owner_id)
        reply = await self.client.post(
            "/api/web/support/messages",
            json={
                "ticket_id": tid,
                "text": "Please check settings",
                "request_id": "fixture-owner-reply",
            },
        )
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()["status"], "waiting_client")
        self.login(self.uid)
        self.assertEqual(
            len(
                (await self.client.get(f"/api/web/support?ticket_id={tid}")).json()[
                    "selected"
                ]["messages"]
            ),
            2,
        )
        self.login(self.owner_id)
        self.assertEqual(
            (await self.client.delete(f"/api/web/support/tickets/{tid}")).status_code,
            200,
        )
        self.login(self.uid)
        self.assertEqual(
            (await self.client.get(f"/api/web/support?ticket_id={tid}")).status_code,
            404,
        )
        async with self.db.sessions() as session:
            self.assertIsNone(await session.get(Ticket, tid))
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(TicketMessage)), 0
            )

    async def test_proxy_checkout_delegates_once_and_preserves_conflict_errors(self):
        result = {
            "status": "pending",
            "payment_id": 55,
            "redirect_url": "https://payment.invalid",
        }
        purchase = AsyncMock(return_value=result)
        plans = AsyncMock(return_value=[SimpleNamespace(id=7, enabled=True)])
        with (
            patch("app.services.proxy_control.plans", plans),
            patch("app.web.proxy_control.purchase", purchase),
        ):
            response = await self.client.post(
                "/api/web/proxy/checkout",
                json={"method": "yookassa", "idempotency_key": "fixture-proxy-request"},
            )
            self.assertEqual(response.json(), result)
            purchase.assert_awaited_once()
            self.assertEqual(purchase.call_args.args[3].plan_id, 7)
            purchase.side_effect = ValueError("Provider not ready")
            response = await self.client.post(
                "/api/web/proxy/checkout",
                json={"method": "balance", "idempotency_key": "fixture-proxy-second"},
            )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"], "Provider not ready")
        async with self.db.sessions() as session:
            self.assertEqual(
                await session.scalar(
                    select(func.count()).select_from(BalanceTransaction)
                ),
                0,
            )

    async def test_ai_dispatch_keeps_support_and_content_handlers(self):
        async with self.db.sessions() as session:
            owner = await session.get(User, self.owner_id)
            app = app_for(session, self.settings, owner)
            paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
        self.assertIn("/api/web/support/tickets/{ticket_id}", paths)
        self.assertIn("/api/web/admin/support/quick-replies", paths)
        self.assertIn("/api/web/admin/links", paths)
        self.assertIn("/api/web/admin/news/{item_id}", paths)

    @unittest.skipUnless(
        os.environ.get("AI_TEST_POSTGRES") == "1", "PostgreSQL row-lock integration"
    )
    async def test_concurrent_withdrawals_cannot_reserve_the_same_balance_twice(self):
        async with self.db.sessions.begin() as session:
            actor = await session.get(User, self.uid)
            session.add(
                BalanceTransaction(
                    user_id=self.uid,
                    type="fixture",
                    amount=100000,
                    description="Test balance",
                )
            )
        first_read, second_read, release = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        original_balance = SQLRepository.balance
        calls = 0

        async def balance(repo, user_id):
            nonlocal calls
            amount = await original_balance(repo, user_id)
            calls += 1
            if calls == 1:
                first_read.set()
                await release.wait()
            else:
                second_read.set()
            return amount

        payload = {
            "bank": "Test bank",
            "owner_name": "Test Person",
            "method": "phone",
            "account": "+79990000000",
            "amount": 100000,
        }
        # Authentication is covered separately. Bypass activity writes here so
        # only the financial handler's own transaction can serialize requests.
        with (
            patch.object(WebContext, "current_user", AsyncMock(return_value=actor)),
            patch.object(SQLRepository, "balance", balance),
        ):
            first = asyncio.create_task(
                self.client.post("/api/web/withdrawals", json=payload)
            )
            await asyncio.wait_for(first_read.wait(), timeout=5)
            second = asyncio.create_task(
                self.client.post("/api/web/withdrawals", json=payload)
            )
            try:
                await asyncio.wait_for(second_read.wait(), timeout=0.3)
            except TimeoutError:
                pass  # A row lock correctly keeps the second read pending.
            finally:
                release.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
        self.assertEqual(sorted(r.status_code for r in results), [200, 409])
        async with self.db.sessions() as session:
            self.assertEqual(
                await original_balance(SQLRepository(session), self.uid), 0
            )
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(Withdrawal)), 1
            )
