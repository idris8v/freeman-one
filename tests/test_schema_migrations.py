"""Real isolated PostgreSQL orchestration; no providers, payments or live data."""

import asyncio
import json
import os
import secrets
import sys
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import httpx
from sqlalchemy import event, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import Settings
from app.database.migrations import (
    LEDGER,
    MIGRATIONS,
    Migration,
    MigrationBusy,
    MigrationError,
    upgrade,
)
from app.database.models import Base
from app.database.schema_readiness import SchemaLease, SchemaNotReady, inspect_schema
from app.database.session import Database
from app.main import create_app


@unittest.skipUnless(
    os.environ.get("AI_TEST_POSTGRES") == "1", "requires isolated PostgreSQL"
)
class SchemaMigrations(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.url = os.environ.get("DATABASE_URL", "")
        parsed = urlsplit(self.url)
        if parsed.scheme not in {
            "postgresql",
            "postgresql+asyncpg",
        } or not parsed.path.lstrip("/").startswith("test_"):
            raise RuntimeError("Use a disposable test_ PostgreSQL database")
        self.schema = "schema_test_" + secrets.token_hex(8)
        self.admin = create_async_engine(self.url, hide_parameters=True)
        async with self.admin.begin() as c:
            await c.exec_driver_sql("CREATE SCHEMA " + self.schema)
        self.db = self.database()
        self.databases = [self.db]
        self.settings = Settings(
            _env_file=None,
            bot_token="",
            database_url=self.url,
            web_base_url="https://example.test",
            encryption_key="",
        )

    def database(self):
        d = Database(self.url)
        # Original pool has never connected. Keep schema ownership local to test.
        d.engine = create_async_engine(
            self.url,
            hide_parameters=True,
            connect_args={"server_settings": {"search_path": self.schema}},
        )
        d.sessions = async_sessionmaker(d.engine, expire_on_commit=False)
        return d

    async def asyncTearDown(self):
        for d in self.databases:
            lease = getattr(d, "schema_lease", None)
            if lease:
                await lease.close()
            await d.close()
        async with self.admin.begin() as c:
            await c.exec_driver_sql("DROP SCHEMA " + self.schema + " CASCADE")
        await self.admin.dispose()

    async def history(self):
        async with self.db.engine.connect() as c:
            if not await c.run_sync(lambda s: inspect(s).has_table(LEDGER.name)):
                return {}
            return dict(
                (await c.execute(select(LEDGER.c.version, LEDGER.c.checksum))).all()
            )

    async def legacy(self):
        # Representative old schema with ownership, paid terms, pending order,
        # uncertain creation and a partially completed renewal, no external calls.
        async with self.db.engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
            await c.exec_driver_sql(
                "INSERT INTO users (id,telegram_id,freeman_id,email,first_name,trial_used,status,registration_source,auth_version,email_verified,created_at,updated_at) VALUES (1,NULL,'FO-1','owner@example.test','Fixture',false,'active','email',0,true,now(),now())"
            )
            await c.exec_driver_sql(
                "INSERT INTO tariffs (id,months,price,device_limit,created_at) VALUES (1,1,40000,2,now())"
            )
            await c.exec_driver_sql(
                "INSERT INTO subscriptions (id,user_id,tariff_id,kind,device_limit,expires_at,created_at) VALUES (1,1,1,'paid',2,'2028-01-31T12:00:00Z',now())"
            )
            await c.exec_driver_sql(
                "INSERT INTO payments (id,user_id,subscription_id,amount,status,provider_id,created_at) VALUES (1,1,1,40000,'order_pending','fixture-provider',now())"
            )
            await c.exec_driver_sql(
                "INSERT INTO app_settings (key,value) VALUES ('order:1','{\"tariff_id\":1,\"months\":1,\"devices\":2,\"amount\":40000}'),('order_idem:1','keep-idempotency')"
            )
            # Journals intentionally have no FKs; these references can coexist
            # with an uncommitted key in the real independent recovery journal.
            await c.exec_driver_sql(
                "INSERT INTO vless_creations (id,user_id,customer_ref,server_id,server_url,request,state,last_error,created_at,updated_at) VALUES ('fixture-create',1,'user:1',7,'https://panel.invalid','{}','outcome_unknown','',now(),now())"
            )
            await c.exec_driver_sql(
                "INSERT INTO vless_renewals (id,user_id,subscription_id,credential_id,key_id,server_id,server_url,remote_identity,request,base_expires_at,base_device_limit,expires_at,device_limit,target_status,state,patch_state,enable_state,last_error,created_at,updated_at) VALUES ('fixture-renew',1,1,10,10,7,'https://panel.invalid','fixture-uuid','{\"payment_id\":1}', '2028-01-31T12:00:00Z',2,'2028-02-29T12:00:00Z',2,'ACTIVE','outcome_unknown','confirmed','outcome_unknown','',now(),now())"
            )
            await c.exec_driver_sql("DROP TABLE vless_reservations")
            await c.exec_driver_sql("DROP TABLE vless_sync")
            await c.exec_driver_sql("DROP TABLE vless_sync_gates")
            await c.exec_driver_sql(
                "ALTER TABLE vless_creations DROP COLUMN dispatch_state"
            )
            await c.exec_driver_sql("ALTER TABLE vless_creations DROP COLUMN placement")
            await c.exec_driver_sql(
                "ALTER TABLE remnawave_servers DROP COLUMN capacity_revision"
            )
            await c.exec_driver_sql(
                "ALTER TABLE tariffs ADD CONSTRAINT old_months_unique UNIQUE(months)"
            )

    async def business_snapshot(self):
        async with self.db.engine.connect() as c:
            return {
                name: [
                    tuple(r)
                    for r in (
                        await c.exec_driver_sql("SELECT * FROM " + name + " ORDER BY 1")
                    )
                ]
                for name in (
                    "users",
                    "tariffs",
                    "subscriptions",
                    "payments",
                    "app_settings",
                    "vless_renewals",
                )
            }

    async def test_fresh_install_and_repeat_are_tracked(self):
        self.assertFalse((await inspect_schema(self.db.engine)).ready)
        self.assertEqual(await upgrade(self.db.engine), [m.version for m in MIGRATIONS])
        before = await self.history()
        self.assertEqual(await upgrade(self.db.engine), [])
        self.assertEqual(before, await self.history())
        self.assertTrue((await inspect_schema(self.db.engine)).ready)

    async def test_lost_final_response_reuses_committed_history(self):
        from app.database import migrations

        original = migrations._upgrade_locked

        async def lost_response(*args, **kwargs):
            await original(*args, **kwargs)
            raise TimeoutError("fixture response lost after final commit")

        with patch.object(migrations, "_upgrade_locked", lost_response):
            with self.assertRaises(TimeoutError):
                await upgrade(self.db.engine)
        self.assertEqual(len(await self.history()), len(MIGRATIONS))
        self.assertEqual(await upgrade(self.db.engine), [])
        self.assertTrue((await inspect_schema(self.db.engine)).ready)

    async def test_old_schema_preserves_paid_state_and_recovery(self):
        await self.legacy()
        before = await self.business_snapshot()
        await upgrade(self.db.engine)
        after = await self.business_snapshot()
        # Only the intentional legacy tariff binding is added.
        settings = after.pop("app_settings")
        old_settings = before.pop("app_settings")
        self.assertEqual(before, after)
        self.assertTrue(set(old_settings).issubset(settings))
        async with self.db.engine.connect() as c:
            self.assertEqual(
                await c.scalar(
                    text(
                        "SELECT state FROM vless_reservations WHERE operation_id='fixture-create'"
                    )
                ),
                "reserved",
            )
            self.assertEqual(
                await c.scalar(text("SELECT dispatch_state FROM vless_creations")),
                "legacy_unknown",
            )
        self.assertTrue((await inspect_schema(self.db.engine)).ready)

    async def fail_at_capacity(self, action):
        original = Migration.apply

        async def injected(migration, engine):
            await original(migration, engine)
            if migration.version == "008_vless_capacity":
                await action(engine)

        return patch.object(Migration, "apply", injected)

    async def assert_capacity_rolled_back(self):
        self.assertEqual(
            list(await self.history()), [m.version for m in MIGRATIONS[:7]]
        )
        async with self.db.engine.connect() as c:
            columns = await c.run_sync(
                lambda s: {
                    v["name"] for v in inspect(s).get_columns("remnawave_servers")
                }
            )
            self.assertNotIn("capacity_revision", columns)
            self.assertEqual(
                await c.scalar(text("SELECT count(*) FROM vless_reservations")), 0
            )

    async def test_interrupted_step_rolls_back_progress_and_tracking(self):
        await self.legacy()

        async def fail(_):
            raise RuntimeError("fixture interruption after DDL/backfill")

        with await self.fail_at_capacity(fail):
            with self.assertRaises(RuntimeError):
                await upgrade(self.db.engine)
        await self.assert_capacity_rolled_back()
        self.assertFalse((await inspect_schema(self.db.engine)).ready)
        await upgrade(self.db.engine)
        self.assertTrue((await inspect_schema(self.db.engine)).ready)

    async def test_cancellation_releases_lock_and_current_transaction(self):
        await self.legacy()
        entered = asyncio.Event()

        async def pause(_):
            entered.set()
            await asyncio.Event().wait()

        with await self.fail_at_capacity(pause):
            task = asyncio.create_task(upgrade(self.db.engine))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await self.assert_capacity_rolled_back()
        await upgrade(self.db.engine)
        self.assertTrue((await inspect_schema(self.db.engine)).ready)

    async def test_lost_postgresql_connection_recovers_after_restart(self):
        await self.legacy()
        entered, release = asyncio.Event(), asyncio.Event()
        backend = []

        async def pause(engine):
            async with engine.connect() as c:
                backend.append(await c.scalar(text("SELECT pg_backend_pid()")))
            entered.set()
            await release.wait()

        with await self.fail_at_capacity(pause):
            task = asyncio.create_task(upgrade(self.db.engine))
            await asyncio.wait_for(entered.wait(), 5)
            async with self.admin.connect() as c:
                await c.execute(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": backend[0]}
                )
            release.set()
            with self.assertRaises(Exception):
                await task
        await self.assert_capacity_rolled_back()
        await self.db.close()
        restarted = self.database()
        self.databases.append(restarted)
        await upgrade(restarted.engine)
        self.assertTrue((await inspect_schema(restarted.engine)).ready)

    async def test_ledger_failure_cannot_commit_migration_without_tracking(self):
        await self.legacy()

        def fail_ledger(conn, cursor, statement, parameters, context, executemany):
            if (
                statement.startswith("INSERT INTO schema_migrations")
                and "008_vless_capacity" in parameters
            ):
                raise RuntimeError("fixture ledger write failure")

        event.listen(self.db.engine.sync_engine, "before_cursor_execute", fail_ledger)
        try:
            with self.assertRaises(RuntimeError):
                await upgrade(self.db.engine)
        finally:
            event.remove(
                self.db.engine.sync_engine, "before_cursor_execute", fail_ledger
            )
        await self.assert_capacity_rolled_back()
        await upgrade(self.db.engine)

    async def test_legacy_internal_commit_is_inside_runner_transaction(self):
        await self.legacy()
        original = Migration.apply

        async def fail_accounts(migration, engine):
            await original(migration, engine)
            if migration.version == "002_accounts":
                async with engine.connect() as c:
                    await c.exec_driver_sql(
                        "UPDATE users SET first_name='must rollback'"
                    )
                    await c.commit()
                raise RuntimeError("after explicit legacy commit")

        with patch.object(Migration, "apply", fail_accounts):
            with self.assertRaises(RuntimeError):
                await upgrade(self.db.engine)
        async with self.db.engine.connect() as c:
            self.assertEqual(
                await c.scalar(text("SELECT first_name FROM users")), "Fixture"
            )
        self.assertEqual(list(await self.history()), [MIGRATIONS[0].version])
        await upgrade(self.db.engine)

    async def test_concurrent_runners_have_one_exclusive_owner(self):
        await self.legacy()
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause(_):
            entered.set()
            await release.wait()

        with await self.fail_at_capacity(pause):
            first = asyncio.create_task(upgrade(self.db.engine))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                with self.assertRaises(MigrationBusy):
                    await upgrade(self.db.engine)
                state = await inspect_schema(self.db.engine)
                self.assertEqual(state.reason, "migration_in_progress")
            finally:
                release.set()
                await first
        self.assertEqual(len(await self.history()), len(MIGRATIONS))
        self.assertEqual(await upgrade(self.db.engine), [])

    async def test_history_checksum_unknown_and_gaps_fail_closed(self):
        await upgrade(self.db.engine)
        async with self.db.engine.begin() as c:
            await c.execute(
                LEDGER.update()
                .where(LEDGER.c.version == MIGRATIONS[0].version)
                .values(checksum="0" * 64)
            )
        self.assertFalse((await inspect_schema(self.db.engine)).ready)
        with self.assertRaises(MigrationError):
            await upgrade(self.db.engine)
        async with self.db.engine.begin() as c:
            await c.execute(
                LEDGER.update()
                .where(LEDGER.c.version == MIGRATIONS[0].version)
                .values(checksum=MIGRATIONS[0].checksum)
            )
            await c.execute(
                LEDGER.insert().values(version="999_future", checksum="0" * 64)
            )
        with self.assertRaises(MigrationError):
            await upgrade(self.db.engine)
        async with self.db.engine.begin() as c:
            await c.execute(
                LEDGER.delete().where(
                    LEDGER.c.version.in_(["999_future", MIGRATIONS[0].version])
                )
            )
        with self.assertRaises(MigrationError):
            await upgrade(self.db.engine)

    async def test_recorded_migrations_do_not_hide_schema_drift(self):
        await upgrade(self.db.engine)
        async with self.db.engine.begin() as c:
            await c.exec_driver_sql("ALTER TABLE vless_creations DROP COLUMN placement")
        state = await inspect_schema(self.db.engine)
        self.assertIn("missing_column:vless_creations.placement", state.issues)
        self.assertFalse(state.ready)

    async def test_pending_renewal_fence_is_checked_semantically(self):
        await upgrade(self.db.engine)
        async with self.db.engine.begin() as c:
            await c.exec_driver_sql("DROP INDEX uq_vless_renewal_pending_subscription")
            await c.exec_driver_sql(
                "CREATE UNIQUE INDEX uq_vless_renewal_pending_subscription ON vless_renewals(subscription_id) WHERE state='applied'"
            )
        state = await inspect_schema(self.db.engine)
        self.assertIn("invalid_fence:vless_renewals", state.issues)
        self.assertFalse(state.ready)
        async with self.db.engine.begin() as c:
            await c.exec_driver_sql("DROP INDEX uq_vless_renewal_pending_subscription")
            await c.exec_driver_sql(
                "CREATE UNIQUE INDEX uq_vless_renewal_pending_subscription ON vless_renewals(subscription_id) WHERE state NOT IN ('applied','failed','patch_confirmed')"
            )
        self.assertIn(
            "invalid_fence:vless_renewals",
            (await inspect_schema(self.db.engine)).issues,
        )

    async def test_shared_runtime_lease_blocks_upgrade_until_shutdown(self):
        await upgrade(self.db.engine)
        first, second = SchemaLease(self.db), SchemaLease(self.db)
        self.assertTrue((await first.open()).ready)
        self.assertTrue((await second.open()).ready)
        try:
            self.assertEqual(self.db.engine.pool.checkedout(), 0)
            with self.assertRaises(MigrationBusy):
                await upgrade(self.db.engine)
            await first.close()
            with self.assertRaises(MigrationBusy):
                await upgrade(self.db.engine)
        finally:
            await first.close()
            await second.close()
        self.assertEqual(await upgrade(self.db.engine), [])

    @asynccontextmanager
    async def running(self, database=None, with_jobs=False):
        from contextlib import ExitStack

        database = database or self.db
        jobs = []

        async def parked(*_args, **_kwargs):
            jobs.append(asyncio.current_task())
            await asyncio.Event().wait()

        targets = (
            "app.web.posts.publication_worker",
            "app.services.customer_controls.worker",
            "app.services.email_delivery.EmailWorker.run",
            "app.services.proxies.ProxyServerWorker.run",
            "app.services.vpn.VpnWorker.run",
            "app.services.remnawave.RemnawaveWorker.run",
        )
        runtime = SimpleNamespace(
            start=lambda: None,
            close=AsyncMock(),
            health=lambda: {"state": "token_missing"},
        )
        with ExitStack() as stack:
            stack.enter_context(patch("app.main.Database", return_value=database))
            stack.enter_context(patch("app.main.seed", new_callable=AsyncMock))
            stack.enter_context(
                patch("app.web.posts.migrate_publications", new_callable=AsyncMock)
            )
            stack.enter_context(
                patch("app.bot.runtime.BotRuntime", return_value=runtime)
            )
            for target in targets:
                stack.enter_context(patch(target, side_effect=parked))
            app = create_app(self.settings)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="https://example.test",
                ) as client:
                    if with_jobs:
                        await asyncio.sleep(0)
                    yield app, client, jobs, runtime

    async def test_unmigrated_startup_is_live_not_ready_and_has_no_writers(self):
        async with self.running() as (_, client, jobs, runtime):
            self.assertEqual((await client.get("/live")).status_code, 200)
            health = await client.get("/health")
            self.assertEqual(health.status_code, 503)
            self.assertEqual(health.json()["status"], "NOT READY")
            self.assertEqual(
                (await client.post("/api/web/checkout", json={})).status_code, 503
            )
            self.assertEqual((await client.get("/")).status_code, 503)
            self.assertEqual(jobs, [])
            runtime.close.assert_not_awaited()
        self.assertEqual(await self.history(), {})

    async def test_startup_during_migration_does_not_process_work(self):
        await self.legacy()
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause(_):
            entered.set()
            await release.wait()

        with await self.fail_at_capacity(pause):
            task = asyncio.create_task(upgrade(self.db.engine))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                async with self.running() as (_, client, jobs, _):
                    self.assertEqual(
                        (await client.get("/health")).json()["schema"]["reason"],
                        "migration_in_progress",
                    )
                    self.assertEqual(jobs, [])
            finally:
                release.set()
                await task

    async def test_concurrent_ready_startup_performs_no_migrations(self):
        await upgrade(self.db.engine)
        second = self.database()
        self.databases.append(second)
        before = await self.history()
        async with self.running(with_jobs=True) as (_, a, jobs_a, _):
            async with self.running(second, with_jobs=True) as (_, b, jobs_b, _):
                self.assertEqual((await a.get("/health")).status_code, 200)
                self.assertEqual((await b.get("/health")).status_code, 200)
                self.assertEqual(len(jobs_a), 6)
                self.assertEqual(len(jobs_b), 6)
                with self.assertRaises(MigrationBusy):
                    await upgrade(second.engine)
        self.assertEqual(before, await self.history())

    async def test_lease_loss_disables_http_and_stops_all_jobs(self):
        await upgrade(self.db.engine)
        async with self.running(with_jobs=True) as (_, client, jobs, runtime):
            lease = self.db.schema_lease
            pid = await lease.connection.scalar(text("SELECT pg_backend_pid()"))
            async with self.admin.connect() as c:
                await c.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            self.assertEqual((await client.get("/health")).status_code, 503)
            self.assertEqual(
                (await client.post("/api/web/checkout", json={})).status_code, 503
            )

            async def stopped():
                while not all(j.done() for j in jobs):
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(stopped(), 6)
            runtime.close.assert_awaited()
        restarted = self.database()
        self.databases.append(restarted)
        async with self.running(restarted) as (_, client, _, _):
            self.assertEqual((await client.get("/health")).status_code, 200)

    async def test_worker_gate_refuses_factory_without_schema(self):
        lease = SchemaLease(self.db)
        factory = AsyncMock()
        with self.assertRaises(SchemaNotReady):
            await lease.run_component(factory)
        factory.assert_not_called()

    @asynccontextmanager
    async def cli_database(self):
        name = "test_schema_cli_" + secrets.token_hex(8)
        admin = create_async_engine(
            self.url, isolation_level="AUTOCOMMIT", hide_parameters=True
        )
        async with admin.connect() as c:
            await c.exec_driver_sql("CREATE DATABASE " + name)
        url = make_url(self.url).set(database=name)
        database = Database(url.render_as_string(hide_password=False))
        try:
            yield database
        finally:
            await database.close()
            async with admin.connect() as c:
                await c.exec_driver_sql("DROP DATABASE " + name + " WITH (FORCE)")
            await admin.dispose()

    async def cli(self, database, command):
        env = {
            **os.environ,
            "DATABASE_URL": database.engine.url.render_as_string(hide_password=False),
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.database.migrations",
            command,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return process

    async def cli_result(self, process):
        stdout, _stderr = await asyncio.wait_for(process.communicate(), 15)
        return process.returncode, json.loads(stdout)

    async def test_operator_cli_check_is_read_only_and_upgrade_repeatable(self):
        async with self.cli_database() as d:
            code, state = await self.cli_result(await self.cli(d, "check"))
            self.assertEqual(code, 1)
            self.assertEqual(state["status"], "NOT READY")
            async with d.engine.connect() as c:
                self.assertEqual(
                    await c.run_sync(lambda s: inspect(s).get_table_names()), []
                )
            code, state = await self.cli_result(await self.cli(d, "upgrade"))
            self.assertEqual(code, 0)
            self.assertEqual(len(state["applied"]), len(MIGRATIONS))
            code, state = await self.cli_result(await self.cli(d, "upgrade"))
            self.assertEqual(code, 0)
            self.assertEqual(state["applied"], [])
            code, state = await self.cli_result(await self.cli(d, "check"))
            self.assertEqual(code, 0)
            self.assertEqual(state["status"], "READY")

    async def test_separate_cli_processes_cannot_migrate_concurrently(self):
        async with self.cli_database() as d:
            async with d.engine.begin() as c:
                await c.run_sync(Base.metadata.create_all)
            async with d.engine.connect() as holder:
                await holder.exec_driver_sql("LOCK TABLE tariffs IN ACCESS SHARE MODE")
                first = await self.cli(d, "upgrade")

                async def reached_tariff_step():
                    while True:
                        async with d.engine.connect() as c:
                            exists = await c.run_sync(
                                lambda s: inspect(s).has_table(LEDGER.name)
                            )
                            if exists and await c.scalar(
                                select(LEDGER.c.version).where(
                                    LEDGER.c.version == "010_vless_fallback"
                                )
                            ):
                                return
                        await asyncio.sleep(0.01)

                try:
                    await asyncio.wait_for(reached_tariff_step(), 10)
                    code, state = await self.cli_result(await self.cli(d, "upgrade"))
                    self.assertEqual(code, 2)
                    self.assertEqual(state["error"], "MigrationBusy")
                    self.assertEqual(state["reason"], "schema_in_use")
                finally:
                    await holder.rollback()
                    code, state = await self.cli_result(first)
                self.assertEqual(code, 0)
                self.assertEqual(len(state["applied"]), len(MIGRATIONS))
            async with d.engine.connect() as c:
                self.assertEqual(
                    len((await c.execute(select(LEDGER))).all()), len(MIGRATIONS)
                )


if __name__ == "__main__":
    unittest.main()
