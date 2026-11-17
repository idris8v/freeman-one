"""Real aiogram lifecycles; isolated DBs and fake Telegram, never live sends."""

import asyncio
import importlib.util
import json
import os
import unittest
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote, urlsplit

import httpx
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import (
    TelegramConflictError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import GetMe, GetUpdates
from aiogram.types import User as TelegramUser
from aiogram.utils.backoff import BackoffConfig
from sqlalchemy import select, text
from test_freeman_ai import Tests as DatabaseFixture

from app.bot import runtime as telegram_runtime
from app.config.settings import Settings
from app.database.models import User
from app.main import create_app
from app.services.dispatch import DispatchWorker

TOKEN = "123456:TEST_TOKEN_NOT_REAL"
REAL_DISPATCH_RUN = DispatchWorker.run


class TelegramSession(BaseSession):
    def __init__(self):
        super().__init__(timeout=0.1)
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.commands_entered, self.commands_release = asyncio.Event(), asyncio.Event()
        self.poll_entered, self.poll_release = asyncio.Event(), asyncio.Event()
        self.hang_identity = self.hang_commands = self.hang_polling = False
        self.identity_error = None
        self.poll_error = None
        self.hang_identity_after = None
        self.identity_errors, self.poll_errors = [], []
        self.calls, self.active = Counter(), Counter()
        self.closed = self.max_polling = 0
        self.username = "fixture_bot"

    async def close(self):
        self.closed += 1

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        name = method.__api_method__
        self.calls[name] += 1
        self.active[name] += 1
        try:
            if name == "getMe":
                self.entered.set()
                if self.hang_identity or (
                    self.hang_identity_after
                    and self.calls[name] >= self.hang_identity_after
                ):
                    await self.release.wait()
                if self.identity_error:
                    raise self.identity_error
                if self.identity_errors:
                    raise self.identity_errors.pop(0)
                return TelegramUser(
                    id=123456, is_bot=True, first_name="Fixture", username=self.username
                )
            if name == "setMyCommands":
                self.commands_entered.set()
                if self.hang_commands:
                    await self.commands_release.wait()
            if name == "getUpdates":
                self.max_polling = max(self.max_polling, self.active[name])
                self.poll_entered.set()
                if self.hang_polling:
                    await self.poll_release.wait()
                if self.poll_errors:
                    raise self.poll_errors.pop(0)
                if self.poll_error:
                    raise self.poll_error
                await asyncio.sleep(0.02)
                return []
            return True
        finally:
            self.active[name] -= 1


class WebsiteStartup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if os.environ.get("AI_TEST_POSTGRES") == "1":
            url = urlsplit(os.environ.get("DATABASE_URL", ""))
            name = unquote(url.path).lstrip("/")
            if url.scheme not in {"postgresql", "postgresql+asyncpg"} or not (
                name.startswith("test_") or name.endswith(("_test", "_tests"))
            ):
                raise RuntimeError("Use a disposable test PostgreSQL database")
        await DatabaseFixture.asyncSetUp(self)
        self.addAsyncCleanup(DatabaseFixture.asyncTearDown, self)
        # Schema upgrades are an explicit pre-start step, never part of lifespan.
        await self.db.initialize()
        self.settings = Settings(
            _env_file=None,
            bot_token=TOKEN,
            bot_username="fixture_bot",
            encryption_key=self.settings.encryption_key,
            database_url=str(self.db.engine.url),
            web_base_url="https://example.test",
        )
        self.telegram = TelegramSession()
        self.bot = Bot(TOKEN, session=self.telegram)
        self.addAsyncCleanup(self.bot.session.close)
        self.jobs = {}
        self.mock("app.main.Database", return_value=self.db)
        self.mock("app.main.Bot", return_value=self.bot)
        for name, value in (
            ("STARTUP_SECONDS", 5),
            ("REQUEST_SECONDS", 5),
            ("POLL_REQUEST_SECONDS", 5),
            ("RETRY_SECONDS", 0.05),
            ("MAX_RETRY_SECONDS", 0.2),
            ("CONFIG_RETRY_SECONDS", 0.5),
            ("HEARTBEAT_SECONDS", 0.1),
            ("DRAIN_SECONDS", 0.25),
        ):
            item = patch.object(telegram_runtime, name, value)
            item.start()
            self.addCleanup(item.stop)
        self.mock(
            "app.bot.runtime.BackoffConfig",
            return_value=BackoffConfig(
                min_delay=0.05, max_delay=0.1, factor=2, jitter=0
            ),
        )
        for target in (
            "app.main.seed",
            "app.services.payment_settings.PaymentSettings.hydrate",
            "app.services.gifts.cancel_legacy_notifications",
            "app.web.posts.migrate_publications",
            "app.services.email_delivery.prepare_mail",
        ):
            self.mock(target, new_callable=AsyncMock)
        item = patch.object(self.db, "initialize", new_callable=AsyncMock)
        item.start()
        self.addCleanup(item.stop)
        for target in (
            "app.web.posts.publication_worker",
            "app.services.customer_controls.worker",
            "app.services.email_delivery.EmailWorker.run",
            "app.services.dispatch.DispatchWorker.run",
            "app.services.offsite_backup.OffsiteBackupWorker.run",
            "app.services.proxies.ProxyServerWorker.run",
            "app.services.vpn.VpnWorker.run",
            "app.services.remnawave.RemnawaveWorker.run",
            "app.services.support_counter.run",
        ):

            async def parked(*args, **kwargs):
                await asyncio.Event().wait()

            self.jobs[target] = self.mock(target, side_effect=parked)

    def mock(self, target, **kwargs):
        item = patch(target, **kwargs)
        result = item.start()
        self.addCleanup(item.stop)
        return result

    @asynccontextmanager
    async def running(self):
        app = create_app(self.settings)
        context = app.router.lifespan_context(app)
        try:
            await asyncio.wait_for(context.__aenter__(), 1)
        except TimeoutError:
            self.fail(
                "Website exceeded the one-second startup budget; "
                f"schema={self.db.schema_lease.state.reason or 'ready'}, "
                f"getMe calls={self.telegram.calls['getMe']}"
            )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=self.settings.web_base_url,
            ) as client:
                yield app, client
        finally:
            await asyncio.wait_for(context.__aexit__(None, None, None), 5)

    async def until(self, condition):
        async def wait():
            while not condition():
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait(), 5)

    async def assert_available(self, client):
        response = await client.get("/health")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "ok")
        self.assertNotIn(TOKEN, response.text)
        self.assertEqual((await client.get("/")).status_code, 200)
        return response.json()

    async def test_hanging_get_me_does_not_block_website(self):
        self.telegram.hang_identity = True
        async with self.running() as (_, client):
            await asyncio.wait_for(self.telegram.entered.wait(), 5)
            await self.assert_available(client)

    async def test_telegram_network_failure_does_not_fail_lifespan(self):
        self.telegram.identity_error = TelegramNetworkError(
            method=GetMe(), message="fixture outage"
        )
        async with self.running() as (_, client):
            await self.assert_available(client)

    @patch.object(telegram_runtime, "STARTUP_SECONDS", 0.2)
    async def test_identity_timeout_is_bounded_and_retried(self):
        self.telegram.hang_identity = True
        async with self.running() as (app, client):
            runtime = app.state.bot_runtime
            await self.until(lambda: runtime.attempts >= 2)
            self.assertFalse(runtime.ready.is_set())
            self.assertEqual(self.telegram.calls["getUpdates"], 0)
            await self.assert_available(client)
        self.assertFalse(any(self.telegram.active.values()))

    @patch.object(telegram_runtime, "STARTUP_SECONDS", 0.2)
    async def test_command_timeout_does_not_block_workers_or_http(self):
        self.telegram.hang_commands = True
        async with self.running() as (app, client):
            await self.until(lambda: app.state.bot_runtime.attempts >= 2)
            await self.assert_available(client)
            for target in (
                "app.services.remnawave.RemnawaveWorker.run",
                "app.services.vpn.VpnWorker.run",
                "app.services.proxies.ProxyServerWorker.run",
                "app.services.dispatch.DispatchWorker.run",
            ):
                self.assertEqual(self.jobs[target].await_count, 1)
            self.assertEqual(self.telegram.calls["getUpdates"], 0)

    async def test_invalid_credentials_are_degraded_without_polling(self):
        self.telegram.identity_error = TelegramUnauthorizedError(
            method=GetMe(), message="fixture unauthorized"
        )
        async with self.running() as (app, client):
            await self.until(lambda: app.state.bot_runtime.reason == "invalid_token")
            data = await self.assert_available(client)
            self.assertEqual(data["bot"], "degraded")
            self.assertIsNotNone(data["bot_health"]["next_retry_at"])
            self.assertEqual(self.telegram.calls["getUpdates"], 0)

    async def test_malformed_token_does_not_abort_website(self):
        self.settings.bot_token = "not-a-token"
        self.mock("app.main.Bot", wraps=Bot)
        async with self.running() as (app, client):
            await self.until(lambda: app.state.bot_runtime.reason == "invalid_token")
            await self.assert_available(client)
            self.assertIsNone(app.state.bot_runtime.bot)

    async def test_wrong_bot_identity_never_polls_or_sends_commands(self):
        self.telegram.username = "other_bot"
        async with self.running() as (app, client):
            await self.until(
                lambda: app.state.bot_runtime.reason == "identity_mismatch"
            )
            await self.assert_available(client)
            self.assertEqual(self.telegram.calls["getUpdates"], 0)
            self.assertEqual(self.telegram.calls["setMyCommands"], 0)

    async def test_network_recovery_starts_one_poller_and_one_router(self):
        self.telegram.identity_errors = [
            TelegramNetworkError(method=GetMe(), message="outage")
        ]
        from app.bot.handlers import create_router

        router = self.mock("app.bot.handlers.create_router", wraps=create_router)
        async with self.running() as (app, client):
            runtime = app.state.bot_runtime
            await self.until(runtime.ready.is_set)
            self.assertGreaterEqual(runtime.attempts, 2)
            self.assertEqual(router.call_count, 1)
            self.assertEqual(self.telegram.max_polling, 1)
            self.assertEqual(
                self.jobs["app.services.dispatch.DispatchWorker.run"].await_count, 1
            )
            self.assertEqual((await self.assert_available(client))["bot"], "polling")
        self.assertFalse(any(self.telegram.active.values()))

    async def test_polling_outage_degrades_health_and_recovers_in_place(self):
        async with self.running() as (app, client):
            runtime = app.state.bot_runtime
            await self.until(runtime.ready.is_set)
            polling = app.state.polling_task
            self.telegram.poll_error = TelegramNetworkError(
                method=GetUpdates(), message="outage"
            )
            await self.until(lambda: runtime.status == "degraded")
            self.assertEqual(
                (await self.assert_available(client))["bot_health"]["reason"],
                "network_or_api_error",
            )
            self.telegram.poll_error = None
            await self.until(runtime.ready.is_set)
            self.assertIs(app.state.polling_task, polling)
            self.assertEqual(runtime.attempts, 1)

    @patch.object(telegram_runtime, "POLL_REQUEST_SECONDS", 0.2)
    async def test_hanging_polling_request_has_deadline_and_safe_shutdown(self):
        self.telegram.hang_polling = True
        async with self.running() as (app, client):
            await self.until(lambda: app.state.bot_runtime.reason == "timeout")
            await self.assert_available(client)
            self.assertEqual(self.telegram.max_polling, 1)
        self.assertFalse(any(self.telegram.active.values()))

    async def test_polling_auth_recovery_preserves_fsm_and_handlers(self):
        async with self.running() as (app, client):
            runtime = app.state.bot_runtime
            await self.until(runtime.ready.is_set)
            dispatcher = runtime.dispatcher
            key = StorageKey(bot_id=self.bot.id, chat_id=123, user_id=123)
            await dispatcher.storage.set_state(key, "fixture_state")
            self.telegram.poll_errors = [
                TelegramUnauthorizedError(
                    method=GetUpdates(), message="expired fixture"
                )
            ]
            await self.until(lambda: runtime.reason == "invalid_token")
            await self.until(lambda: runtime.attempts >= 2 and runtime.ready.is_set())
            self.assertIs(runtime.dispatcher, dispatcher)
            self.assertEqual(await dispatcher.storage.get_state(key), "fixture_state")
            self.assertEqual(len(dispatcher.sub_routers), 1)
            self.assertEqual(self.telegram.max_polling, 1)
            await self.assert_available(client)

    async def test_foreign_polling_conflict_pauses_without_extra_instances(self):
        self.telegram.poll_errors = [
            TelegramConflictError(method=GetUpdates(), message="foreign poller")
        ]
        async with self.running() as (app, client):
            await self.until(lambda: app.state.bot_runtime.reason == "polling_conflict")
            self.assertFalse(app.state.bot_runtime.ready.is_set())
            self.assertEqual(self.telegram.max_polling, 1)
            await self.assert_available(client)

    async def test_rate_limit_is_respected_without_overlapping_pollers(self):
        self.telegram.poll_errors = [
            TelegramRetryAfter(
                method=GetUpdates(), message="fixture limit", retry_after=1
            )
        ]
        async with self.running() as (app, client):
            runtime = app.state.bot_runtime
            await self.until(lambda: runtime.reason == "rate_limited")
            self.assertEqual(runtime.server_retry_seconds, 1)
            self.assertFalse(runtime.ready.is_set())
            await self.assert_available(client)
            self.assertEqual(self.telegram.max_polling, 1)

    async def test_repeated_start_calls_return_same_supervisor(self):
        async with self.running() as (app, _):
            runtime = app.state.bot_runtime
            original = runtime.task

            async def start():
                return runtime.start()

            self.assertTrue(
                all(
                    task is original
                    for task in await asyncio.gather(*(start() for _ in range(5)))
                )
            )
            await self.until(runtime.ready.is_set)
            self.assertEqual(self.telegram.max_polling, 1)

    async def test_concurrent_app_startup_has_one_owner_and_takeover(self):
        async with self.running() as (first, first_client):
            await self.until(first.state.bot_runtime.ready.is_set)
            second_session = TelegramSession()
            second_bot = Bot(TOKEN, session=second_session)
            self.mock("app.main.Bot", return_value=second_bot)
            async with self.running() as (second, client):
                await self.until(lambda: second.state.bot_runtime.status == "standby")
                await self.assert_available(first_client)
                await self.assert_available(client)
                self.assertEqual(second_session.calls["getMe"], 0)
                await first.state.bot_runtime.close()
                await self.until(second.state.bot_runtime.ready.is_set)
                self.assertFalse(any(self.telegram.active.values()))
                self.assertEqual(second_session.max_polling, 1)

    async def test_shutdown_drains_handlers_before_releasing_ownership(self):
        async with self.running() as (app, _):
            runtime = app.state.bot_runtime
            await self.until(runtime.ready.is_set)
            release = asyncio.Event()
            task = asyncio.create_task(release.wait())
            runtime.dispatcher._handle_update_tasks.add(task)
            with patch.object(telegram_runtime, "DRAIN_SECONDS", 2):
                closing = asyncio.create_task(runtime.close())
                try:
                    await asyncio.sleep(0.02)
                    self.assertFalse(closing.done())
                    async with telegram_runtime.ownership(self.db) as owner:
                        self.assertIsNone(owner)
                finally:
                    release.set()
                await asyncio.wait_for(closing, 5)
            async with telegram_runtime.ownership(self.db) as owner:
                self.assertIsNotNone(owner)
            self.assertTrue(task.done())
            self.assertFalse(any(self.telegram.active.values()))

    async def test_shutdown_cancels_stuck_handler_and_releases_customer_lock(self):
        async with self.running() as (app, _):
            runtime = app.state.bot_runtime
            await self.until(runtime.ready.is_set)
            entered = asyncio.Event()

            async def handler():
                async with self.db.sessions.begin() as session:
                    user = await session.get(User, self.uid, with_for_update=True)
                    user.first_name = "uncommitted fixture"
                    entered.set()
                    await asyncio.Event().wait()

            task = asyncio.create_task(handler())
            runtime.dispatcher._handle_update_tasks.add(task)
            await entered.wait()
            await asyncio.wait_for(runtime.close(), 5)
            self.assertTrue(task.cancelled())
            async with self.db.sessions.begin() as session:
                user = await session.scalar(
                    select(User).where(User.id == self.uid).with_for_update(nowait=True)
                )
                self.assertNotEqual(user.first_name, "uncommitted fixture")

    async def test_concurrent_shutdown_waits_for_one_cleanup(self):
        async with self.running() as (app, _):
            runtime = app.state.bot_runtime
            await self.until(runtime.ready.is_set)
            release = asyncio.Event()
            handler = asyncio.create_task(release.wait())
            runtime.dispatcher._handle_update_tasks.add(handler)
            with patch.object(telegram_runtime, "DRAIN_SECONDS", 2):
                closing = asyncio.gather(runtime.close(), runtime.close())
                await asyncio.sleep(0.02)
                self.assertFalse(closing.done())
                release.set()
                await asyncio.wait_for(closing, 5)
            self.assertTrue(handler.done())
            self.assertFalse(any(self.telegram.active.values()))

    async def test_financial_preparation_continues_without_claiming_deliveries(self):
        worker = DispatchWorker(self.db, None, self.settings)
        prepared = asyncio.Event()
        worker.prepare = AsyncMock(side_effect=lambda: prepared.set())
        worker.once = AsyncMock()
        worker.notices.once = AsyncMock()
        worker.notices.forum.close_once = AsyncMock()
        ready = asyncio.Event()
        task = asyncio.create_task(REAL_DISPATCH_RUN(worker, telegram_ready=ready))
        try:
            await asyncio.wait_for(prepared.wait(), 5)
            await asyncio.sleep(0)
            worker.once.assert_not_awaited()
            worker.notices.once.assert_not_awaited()
            ready.set()
            await self.until(lambda: worker.notices.once.await_count > 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @unittest.skipUnless(
        os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL"
    )
    async def test_owner_connection_is_autocommit_and_does_not_use_request_pool(self):
        self.telegram.hang_identity = True
        async with self.running() as (app, _):
            await self.until(lambda: app.state.bot_runtime.leader)
            await self.telegram.entered.wait()
            self.assertEqual(self.db.engine.pool.checkedout(), 0)
            async with self.pg_admin.connect() as connection:
                rows = (
                    await connection.execute(
                        text(
                            "SELECT state,xact_start FROM pg_stat_activity WHERE datname=current_database() AND application_name='freemanone-telegram-owner'"
                        )
                    )
                ).all()
            self.assertEqual(len(rows), 1)
            self.assertTrue(all(state != "idle in transaction" for state, _ in rows))
            async with self.db.sessions.begin() as session:
                self.assertEqual(await session.scalar(text("SELECT 1")), 1)

    @patch.object(telegram_runtime, "REQUEST_SECONDS", 0.2)
    async def test_hidden_aiogram_identity_request_is_bounded(self):
        self.telegram.hang_identity_after = 2
        async with self.running() as (app, client):
            await self.until(lambda: app.state.bot_runtime.attempts >= 2)
            self.assertEqual(self.telegram.calls["getUpdates"], 0)
            await self.assert_available(client)
        self.assertFalse(any(self.telegram.active.values()))

    async def test_missing_token_keeps_website_and_non_telegram_jobs_available(self):
        self.settings.bot_token = ""
        async with self.running() as (app, client):
            await self.until(lambda: app.state.bot_runtime.leader)
            self.assertEqual(
                (await self.assert_available(client))["bot"], "token_missing"
            )
            self.assertEqual(self.telegram.calls["getMe"], 0)
            self.assertEqual(
                self.jobs["app.services.remnawave.RemnawaveWorker.run"].await_count, 1
            )

    async def test_real_database_failure_still_marks_website_unhealthy(self):
        async with self.running() as (app, client):
            await self.until(app.state.bot_runtime.ready.is_set)
            with patch.object(
                self.db, "sessions", side_effect=RuntimeError("fixture DB failure")
            ):
                response = await client.get("/health")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["database"], "unavailable")

    @unittest.skipUnless(
        os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL"
    )
    async def test_lost_owner_connection_stops_polling_then_recovers(self):
        ownership = telegram_runtime.ownership
        backend = []

        @asynccontextmanager
        async def tracked(database):
            async with ownership(database) as owner:
                if owner is not None:
                    backend.append(await owner.scalar(text("SELECT pg_backend_pid()")))
                yield owner

        with patch.object(telegram_runtime, "ownership", tracked):
            async with self.running() as (app, client):
                runtime = app.state.bot_runtime
                await self.until(runtime.ready.is_set)
                original = app.state.polling_task
                async with self.pg_admin.begin() as connection:
                    await connection.execute(
                        text("SELECT pg_terminate_backend(:pid)"), {"pid": backend[0]}
                    )
                await self.until(lambda: original.done())
                await self.until(
                    lambda: (
                        runtime.ready.is_set()
                        and app.state.polling_task is not original
                    )
                )
                await self.assert_available(client)
                self.assertEqual(self.telegram.max_polling, 1)

    @unittest.skipUnless(
        os.environ.get("AI_TEST_POSTGRES") == "1", "requires PostgreSQL"
    )
    async def test_standby_does_not_retain_an_extra_database_connection(self):
        async with self.running() as (app, _):
            await self.until(app.state.bot_runtime.ready.is_set)
            async with telegram_runtime.ownership(self.db) as owner:
                self.assertIsNone(owner)
                async with self.pg_admin.connect() as connection:
                    count = await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND application_name='freemanone-telegram-owner'"
                        )
                    )
                self.assertEqual(count, 1)


class BotMonitoring(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "scripts/freeman-monitor.py"
        spec = importlib.util.spec_from_file_location("fixture_monitor", path)
        self.monitor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.monitor)

    def check(self, state):
        response = SimpleNamespace(
            status=200, read=lambda: json.dumps(dict(status="ok", bot=state)).encode()
        )

        class Context:
            def __enter__(self):
                return response

            def __exit__(self, *args):
                pass

        with (
            patch.object(
                self.monitor.urllib.request, "urlopen", return_value=Context()
            ),
            patch.object(
                self.monitor.subprocess,
                "run",
                side_effect=[
                    SimpleNamespace(returncode=0, stdout="bot\n"),
                    SimpleNamespace(stdout="active\n"),
                ],
            ),
            patch.object(
                self.monitor.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=100, total=100),
            ),
        ):
            return self.monitor.check()

    def test_bot_degradation_alert_distinguishes_website_availability(self):
        self.assertEqual(
            self.check("degraded"), ["веб-сайт доступен; Telegram-бот: degraded"]
        )

    def test_polling_and_standby_are_not_reported_as_website_outages(self):
        self.assertEqual(self.check("polling"), [])
        self.assertEqual(self.check("standby"), [])


class TelegramRequestBoundaries(unittest.IsolatedAsyncioTestCase):
    async def test_send_and_upload_requests_keep_their_existing_timeout_budget(self):
        from aiogram.methods import SendDocument

        arrived, release = asyncio.Event(), asyncio.Event()

        async def upload(bot, method):
            arrived.set()
            await release.wait()
            return True

        middleware = telegram_runtime.PollingHealth(SimpleNamespace())
        with patch.object(telegram_runtime, "REQUEST_SECONDS", 0.001):
            task = asyncio.create_task(
                middleware(upload, None, SendDocument(chat_id=1, document="fixture"))
            )
            try:
                await asyncio.wait_for(arrived.wait(), 1)
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
            finally:
                release.set()
                self.assertTrue(await task)
