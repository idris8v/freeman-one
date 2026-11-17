"""Recover Telegram independently of HTTP, with one owner per database."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import (
    TelegramConflictError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand
from aiogram.utils.backoff import BackoffConfig
from aiogram.utils.token import TokenValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

STARTUP_SECONDS = 10
REQUEST_SECONDS = 15
POLL_SECONDS = 10
POLL_REQUEST_SECONDS = 30
RETRY_SECONDS = 5
MAX_RETRY_SECONDS = 60
CONFIG_RETRY_SECONDS = 300
HEARTBEAT_SECONDS = 5
DRAIN_SECONDS = 15
logger = logging.getLogger(__name__)


class RuntimeDispatcher(Dispatcher):
    async def feed_update(self, bot, update, **kwargs):
        query = (update.model_extra or {}).get('widget_query')
        if query is not None:
            from app.services.bot_widget_queries import handle
            return await handle(self.widget_runtime, bot, query)
        if (update.model_extra or {}).get('shared_widget_query') is not None:
            return  # Private purchase widgets never enable forwarded queries.
        return await super().feed_update(bot, update, **kwargs)

    async def emit_shutdown(self, *args, **kwargs):
        # aiogram owns polling tasks, but its detached update handlers must finish
        # before FSM storage closes or another owner starts serving updates.
        handlers = tuple(self._handle_update_tasks)
        if handlers:
            _, pending = await asyncio.wait(handlers, timeout=DRAIN_SECONDS)
            for task in pending:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
        await super().emit_shutdown(*args, **kwargs)


@asynccontextmanager
async def ownership(database):
    """One purposeful, autocommit connection; never borrow the request pool."""
    if database.engine.dialect.name != "postgresql":
        # Preserve SQLite development installs, including overlapping processes.
        import fcntl
        from pathlib import Path

        path = database.engine.url.database
        if not path or path == ":memory:":
            raise ValueError("Telegram ownership requires a persistent database")
        with Path(path + ".telegram.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield None
                return
            try:
                yield lock
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        return

    async with database.sessions() as session:
        path = await session.scalar(text("SHOW search_path"))
    engine = create_async_engine(
        database.engine.url,
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
        connect_args={
            "timeout": 5,
            "command_timeout": 5,
            "server_settings": {
                "search_path": path,
                "application_name": "freemanone-telegram-owner",
            },
        },
    )
    try:
        async with engine.connect() as connection:
            acquired = await connection.scalar(
                text(
                    "SELECT pg_try_advisory_lock(hashtextextended('freemanone:telegram:' || current_schema(), 0))"
                )
            )
            if not acquired:
                await connection.close()  # Standby retry sleeps need no DB resource.
            # NullPool physically closes this session, releasing its lock only
            # after polling, handlers and Telegram jobs have all stopped.
            yield connection if acquired else None
    finally:
        await engine.dispose()


class PollingHealth(BaseRequestMiddleware):
    def __init__(self, runtime):
        self.runtime = runtime

    async def __call__(self, make_request, bot, method):
        polling = method.__api_method__ == "getUpdates"
        if not polling and method.__api_method__ not in {"getMe", "setMyCommands"}:
            # Preserve request-specific send/upload budgets. Only bootstrap and
            # polling are part of this lifecycle's timeout policy.
            return await make_request(bot, method)
        try:
            if polling:
                from app.services.bot_widget_queries import poll
                request = poll(bot, method)
            else:
                request = make_request(bot, method)
            result = await asyncio.wait_for(
                request,
                POLL_REQUEST_SECONDS if polling else REQUEST_SECONDS,
            )
        except Exception as error:
            if polling and self.runtime.accept_updates:
                self.runtime.failed(error)
                # aiogram logs polling exceptions; never include token-bearing URLs.
                raise TelegramNetworkError(
                    method=method, message=type(error).__name__
                ) from None
            raise
        if polling:
            if not self.runtime.accept_updates or not self.runtime.leader:
                return []  # Leave these updates unacknowledged for the next owner.
            self.runtime.ready.set()
            self.runtime.status, self.runtime.reason = "polling", ""
            self.runtime.last_success_at = datetime.now(timezone.utc).isoformat()
            self.runtime.failures = 0
        return result


class BotRuntime:
    def __init__(self, database, settings, state, *, bot_factory=Bot):
        self.database, self.settings, self.state = database, settings, state
        self.bot_factory = bot_factory
        self.bot = self.dispatcher = self.task = None
        self.ready, self.restart = asyncio.Event(), asyncio.Event()
        self.status = "starting" if settings.bot_token else "token_missing"
        self.reason = ""
        self.last_success_at = None
        self.next_retry_at = None
        self.failures = self.attempts = 0
        self.leader = False
        self.accept_updates = False
        self.server_retry_seconds = 0

    def health(self):
        return dict(
            state=self.status,
            reason=self.reason,
            leader=self.leader,
            attempts=self.attempts,
            last_success_at=self.last_success_at,
            next_retry_at=self.next_retry_at,
        )

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name="telegram-runtime")
        return self.task

    def failed(self, error):
        self.ready.clear()
        self.status = "degraded"
        self.reason = (
            "invalid_token"
            if isinstance(error, TelegramUnauthorizedError)
            else "polling_conflict"
            if isinstance(error, TelegramConflictError)
            else "timeout"
            if isinstance(error, TimeoutError)
            else "rate_limited"
            if isinstance(error, TelegramRetryAfter)
            else "network_or_api_error"
        )
        if isinstance(error, TelegramRetryAfter):
            self.server_retry_seconds = error.retry_after
        if self.reason in {"invalid_token", "polling_conflict", "rate_limited"}:
            self.restart.set()

    async def pause(self, delay):
        self.next_retry_at = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + delay, timezone.utc
        ).isoformat()
        await asyncio.sleep(delay)
        self.next_retry_at = None

    def configure(self):
        if not self.settings.bot_token:
            self.status = "token_missing"
            return
        try:
            self.bot = self.bot_factory(self.settings.bot_token)
        except (ValueError, TokenValidationError):
            self.status, self.reason = "degraded", "invalid_token"
            return
        from app.bot.api_timing import ApiTiming
        from app.bot.handlers import create_router
        from app.bot.middlewares import ServicesMiddleware
        from app.services.integrations import (
            MockPaymentService,
            MockProvisioningService,
        )

        self.bot.session.middleware(ApiTiming())
        self.bot.session.middleware(PollingHealth(self))
        storage = (
            RedisStorage.from_url(self.settings.redis_url)
            if self.settings.redis_url
            else MemoryStorage()
        )
        isolation = (
            storage.create_isolation()
            if self.settings.redis_url
            else SimpleEventIsolation()
        )
        self.dispatcher = RuntimeDispatcher(storage=storage, events_isolation=isolation)
        self.dispatcher.widget_runtime = self
        from app.bot.latency import EarlyFeedback,install_sql
        install_sql(self.database.engine)
        # Receive/ACK before the per-customer FSM lock; keep that lock for safety.
        self.dispatcher.update.outer_middleware.unregister(self.dispatcher.fsm)
        self.dispatcher.update.outer_middleware(EarlyFeedback())
        self.dispatcher.update.outer_middleware(self.dispatcher.fsm)
        router = create_router()
        middleware = ServicesMiddleware(
            self.database,
            self.settings,
            MockPaymentService(),
            MockProvisioningService(),
        )
        router.message.outer_middleware(middleware)
        router.callback_query.outer_middleware(middleware)
        router.pre_checkout_query.outer_middleware(middleware)
        self.dispatcher.include_router(router)

    async def heartbeat(self, connection):
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            if self.database.engine.dialect.name == "postgresql":
                await connection.execute(text("SELECT 1"))

    async def stop_polling(self, task):
        if not task.done():
            try:
                await asyncio.wait_for(
                    self.dispatcher.stop_polling(), DRAIN_SECONDS + 5
                )
            except (RuntimeError, TimeoutError):
                task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def attempt(self):
        self.attempts += 1
        self.restart.clear()
        self.server_retry_seconds = 0
        self.status, self.reason = "starting", ""
        identity = await asyncio.wait_for(self.bot.get_me(), STARTUP_SECONDS)
        if (
            not identity.username
            or identity.username.lower() != self.settings.bot_username.lower()
        ):
            self.reason = "identity_mismatch"
            raise ValueError("Bot identity mismatch")
        await asyncio.wait_for(
            self.bot.set_my_commands(
                [
                    BotCommand(command="start", description="Главное меню"),
                    BotCommand(command="help", description="Помощь"),
                    BotCommand(command="paysupport", description="Поддержка по оплате"),
                ]
            ),
            STARTUP_SECONDS,
        )
        from app.services.offsite_backup import OffsiteBackupWorker
        from app.services.support_counter import run as counter_run

        self.accept_updates = True
        polling = asyncio.create_task(
            self.dispatcher.start_polling(
                self.bot,
                handle_signals=False,
                close_bot_session=False,
                polling_timeout=POLL_SECONDS,
                allowed_updates=self.dispatcher.resolve_used_update_types()+['widget_query','shared_widget_query'],
                backoff_config=BackoffConfig(
                    min_delay=1, max_delay=30, factor=2, jitter=0.1
                ),
            ),
            name="telegram-polling",
        )
        self.state.polling_task = polling
        restart = asyncio.create_task(self.restart.wait())
        jobs = [
            asyncio.create_task(counter_run(self.database, self.bot, self.settings)),
            asyncio.create_task(
                OffsiteBackupWorker(self.database, self.bot, self.settings).run()
            ),
        ]
        self.state.backup_worker_task = jobs[1]
        try:
            done, _ = await asyncio.wait(
                (polling, restart), return_when=asyncio.FIRST_COMPLETED
            )
            if polling in done:
                await polling
                self.reason = "polling_stopped"
            raise RuntimeError("Telegram polling needs recovery")
        finally:
            self.accept_updates = False
            self.ready.clear()
            restart.cancel()
            for job in jobs:
                job.cancel()
            await asyncio.gather(restart, *jobs, return_exceptions=True)
            await self.stop_polling(polling)
            self.state.polling_task = self.state.backup_worker_task = None

    async def connect(self):
        if self.bot is None:
            await asyncio.Event().wait()  # Configuration changes require a restart.
        while True:
            try:
                await self.attempt()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self.reason not in {
                    "identity_mismatch",
                    "polling_conflict",
                    "invalid_token",
                    "polling_stopped",
                    "rate_limited",
                }:
                    self.failed(error)
                self.status = "degraded"
                self.failures += 1
                delay = (
                    CONFIG_RETRY_SECONDS
                    if self.reason
                    in {"identity_mismatch", "invalid_token", "polling_conflict"}
                    else min(
                        MAX_RETRY_SECONDS,
                        RETRY_SECONDS * 2 ** min(self.failures - 1, 4),
                    )
                )
                delay = max(delay, self.server_retry_seconds)
                logger.warning("Telegram recovery scheduled: %s", self.reason)
                await self.pause(delay)

    async def run(self):
        from app.database.schema_readiness import SchemaNotReady

        lease = getattr(self.database, "schema_lease", None)
        if lease is None:
            raise SchemaNotReady("schema_not_checked")
        lease.require_ready()
        try:
            try:
                self.configure()
            except Exception:
                self.status, self.reason = "degraded", "configuration_error"
                logger.warning(
                    "Telegram configuration failed; website remains available"
                )
                if self.bot:
                    try:
                        await asyncio.wait_for(self.bot.session.close(), DRAIN_SECONDS)
                    except Exception:
                        logger.warning("Telegram configuration cleanup failed")
                self.bot = None
            from app.services.dispatch import DispatchWorker

            while True:
                try:
                    async with ownership(self.database) as connection:
                        if connection is None:
                            self.status, self.reason = "standby", "another_owner"
                            await self.pause(RETRY_SECONDS)
                            continue
                        self.leader = True
                        try:
                            async with asyncio.TaskGroup() as tasks:
                                tasks.create_task(self.heartbeat(connection))
                                job = tasks.create_task(
                                    DispatchWorker(
                                        self.database, self.bot, self.settings
                                    ).run(telegram_ready=self.ready)
                                )
                                self.state.worker_task = job
                                await self.connect()
                        finally:
                            self.leader = False
                            self.ready.clear()
                            self.state.worker_task = None
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.status, self.reason = "degraded", "ownership_or_worker_error"
                    logger.warning("Telegram owner unavailable; retry scheduled")
                    await self.pause(MAX_RETRY_SECONDS)
        finally:
            self.ready.clear()
            self.status = "stopped"
            for close in (
                self.dispatcher.fsm.close if self.dispatcher else None,
                self.bot.session.close if self.bot else None,
            ):
                if close:
                    try:
                        await asyncio.wait_for(close(), DRAIN_SECONDS)
                    except Exception:
                        logger.warning("Telegram resource cleanup failed")

    async def close(self):
        if self.task:
            if not self.task.done() and not self.task.cancelling():
                self.task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.shield(self.task)
