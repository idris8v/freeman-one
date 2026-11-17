"""Customer key rename through real HTTP routes and disposable PostgreSQL/SQLite."""

import asyncio
import unittest
from datetime import timedelta

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import func, select
from test_freeman_ai import Tests as DatabaseFixture

from app.config.settings import Settings
from app.database.models import AccessKey, AdminAudit, Payment, Subscription, User, now
from app.database.repositories import SQLRepository
from app.services.screens import ScreenService
from app.web.cabinet.context import WebContext
from app.web.routes import website_router
from app.web.security import SecurityMiddleware


class KeyRename(unittest.IsolatedAsyncioTestCase):
    asyncSetUpDatabase = DatabaseFixture.asyncSetUp
    asyncTearDownDatabase = DatabaseFixture.asyncTearDown

    async def asyncSetUp(self):
        await self.asyncSetUpDatabase()
        self.settings = Settings(
            _env_file=None,
            encryption_key=Fernet.generate_key().decode(),
            bot_token="",
            admin_ids="999",
            web_base_url="https://example.test",
            freeman_ai_enabled=False,
        )
        self.context = WebContext(self.db, self.settings)
        app = FastAPI()
        app.include_router(website_router(self.db, self.settings))
        app.add_middleware(SecurityMiddleware, web_base_url=self.settings.web_base_url)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url=self.settings.web_base_url,
        )
        self.accounts = {}
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            for kind, telegram_id, email, freeman_id in (
                ("email", None, "email@example.test", "FO-11"),
                ("linked", 711, "linked@example.test", "FO-12"),
                ("telegram", 712, None, "FO-13"),
                ("legacy", 713, None, None),
            ):
                user = await repo.add(
                    User(
                        telegram_id=telegram_id,
                        email=email,
                        email_verified=bool(email),
                        freeman_id=freeman_id,
                        first_name=kind,
                        registration_source="website_email" if email else "telegram",
                    )
                )
                sub = await repo.add(
                    Subscription(user_id=user.id, expires_at=now() + timedelta(days=30))
                )
                key = await repo.add(
                    AccessKey(
                        user_id=user.id, name="Original", expires_at=sub.expires_at
                    )
                )
                await repo.set_setting(f"subscription_key:{sub.id}", str(key.id))
                self.accounts[kind] = (user.id, key.id, sub.id, telegram_id)
        self.login(self.accounts["email"][0])

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.asyncTearDownDatabase()

    def login(self, user_id, version=0):
        self.client.cookies.set(
            "freeman_session", self.context.session_token(user_id, version)
        )

    async def rename(self, key_id, name):
        return await self.client.put(
            f"/api/web/keys/{key_id}/name", json={"name": name}
        )

    async def assert_persisted(self, kind, name):
        uid, kid, sid, _ = self.accounts[kind]
        async with self.db.sessions() as session:
            repo = SQLRepository(session)
            self.assertEqual((await session.get(AccessKey, kid)).name, name)
            self.assertEqual(await repo.setting(f"subscription_name:{sid}"), name)
            sub = await session.get(Subscription, sid)
            screens = ScreenService(repo, self.settings, None, None)
            self.assertEqual(await screens.subscription_name(sub), name)
            self.assertIn(name[:28], await screens.subscription_list_label(sub))
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(Payment)), 0
            )
        self.login(uid)
        me = await self.client.get("/api/web/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(
            next(k for k in me.json()["keys"] if k["id"] == kid)["name"], name
        )

    async def check_account(self, kind):
        uid, kid, _, telegram_id = self.accounts[kind]
        self.login(uid)
        response = await self.rename(kid, "  Мой телефон 📱  ")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"id": kid, "name": "Мой телефон 📱"})
        await self.assert_persisted(kind, "Мой телефон 📱")
        async with self.db.sessions() as session:
            audit = await session.scalar(
                select(AdminAudit).where(AdminAudit.target == str(kid))
            )
            self.assertEqual(audit.actor_telegram_id, telegram_id or 0)
            self.assertIn(f"user_id={uid}", audit.details)
            self.assertEqual((await session.get(User, uid)).telegram_id, telegram_id)

    async def test_email_only_customer(self):
        await self.check_account("email")

    async def test_telegram_linked_customer(self):
        await self.check_account("linked")

    async def test_telegram_only_customer(self):
        await self.check_account("telegram")

    async def test_existing_customer_without_freeman_id(self):
        await self.check_account("legacy")

    async def test_foreign_keys_are_private_for_all_account_types(self):
        for kind, (uid, _, _, _) in self.accounts.items():
            self.login(uid)
            for other, (_, kid, _, _) in self.accounts.items():
                if other == kind:
                    continue
                response = await self.rename(kid, "Unauthorized")
                self.assertEqual(response.status_code, 404, response.text)
        async with self.db.sessions() as session:
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(AdminAudit)), 0
            )
            self.assertEqual(
                set(await session.scalars(select(AccessKey.name))), {"Original"}
            )

    async def test_deleted_missing_and_unauthenticated_keys(self):
        uid, kid, _, _ = self.accounts["email"]
        async with self.db.sessions.begin() as session:
            (await session.get(AccessKey, kid)).status = "deleted"
        for key_id in (kid, 999999):
            self.assertEqual((await self.rename(key_id, "Hidden")).status_code, 404)
        self.client.cookies.clear()
        self.assertEqual((await self.rename(kid, "Hidden")).status_code, 401)
        self.login(uid, version=9)
        self.assertEqual((await self.rename(kid, "Hidden")).status_code, 401)

    async def test_validation_does_not_mutate_key_or_subscription(self):
        _, kid, sid, _ = self.accounts["email"]
        for name in (
            "",
            "   ",
            "a" * 61,
            "phone\nname",
            "phone\tname",
            "phone\x00name",
            None,
            42,
        ):
            response = await self.rename(kid, name)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn("detail", response.json())
        response = await self.client.put(f"/api/web/keys/{kid}/name", json={})
        self.assertEqual(response.status_code, 422)
        async with self.db.sessions() as session:
            self.assertEqual((await session.get(AccessKey, kid)).name, "Original")
            self.assertEqual(
                await SQLRepository(session).setting(f"subscription_name:{sid}"), ""
            )
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(AdminAudit)), 0
            )

    async def test_sixty_character_name_and_markup_remain_plain_text(self):
        _, kid, _, _ = self.accounts["email"]
        for name in ("я" * 60, '<img src=x onerror="alert(1)">'):
            response = await self.rename(kid, name)
            self.assertEqual(response.status_code, 200, response.text)
            await self.assert_persisted("email", name)

    async def test_retry_after_committed_response_is_lost(self):
        _, kid, _, _ = self.accounts["email"]
        first = await self.rename(kid, "Retry name")
        self.assertEqual(first.status_code, 200, first.text)
        retry = await self.rename(kid, "Retry name")
        self.assertEqual(first.json(), retry.json())
        await self.assert_persisted("email", "Retry name")

    async def test_concurrent_names_leave_key_and_subscription_consistent(self):
        _, kid, sid, _ = self.accounts["email"]
        results = await asyncio.gather(
            self.rename(kid, "First"), self.rename(kid, "Second")
        )
        self.assertEqual(
            [r.status_code for r in results], [200, 200], [r.text for r in results]
        )
        async with self.db.sessions() as session:
            key = await session.get(AccessKey, kid)
            self.assertIn(key.name, ("First", "Second"))
            self.assertEqual(
                await SQLRepository(session).setting(f"subscription_name:{sid}"),
                key.name,
            )

    async def test_postgres_audit_failure_rolls_back_then_retry_recovers(self):
        if self.db.engine.dialect.name != "postgresql":
            self.skipTest("Real PostgreSQL transaction failure regression")
        _, kid, sid, _ = self.accounts["email"]
        async with self.db.engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE FUNCTION reject_rename_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'isolated audit failure'; END $$"
            )
            await connection.exec_driver_sql(
                "CREATE TRIGGER reject_rename_audit BEFORE INSERT ON admin_audit FOR EACH ROW WHEN (NEW.action = 'client_key_rename') EXECUTE FUNCTION reject_rename_audit()"
            )
        try:
            response = await self.rename(kid, "Must roll back")
            self.assertEqual(response.status_code, 500)
            async with self.db.sessions() as session:
                self.assertEqual((await session.get(AccessKey, kid)).name, "Original")
                self.assertEqual(
                    await SQLRepository(session).setting(f"subscription_name:{sid}"), ""
                )
                self.assertEqual(
                    await session.scalar(select(func.count()).select_from(AdminAudit)),
                    0,
                )
        finally:
            async with self.db.engine.begin() as connection:
                await connection.exec_driver_sql(
                    "DROP TRIGGER reject_rename_audit ON admin_audit"
                )
                await connection.exec_driver_sql("DROP FUNCTION reject_rename_audit()")
        response = await self.rename(kid, "Recovered")
        self.assertEqual(response.status_code, 200, response.text)
        await self.assert_persisted("email", "Recovered")
