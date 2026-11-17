"""Offline, ordered migrations: python -m app.database.migrations upgrade|check.

PostgreSQL steps and their ledger entries commit atomically on the *same*
session that owns the exclusive advisory lock. No app/bot/provider startup.
"""

import argparse
import asyncio
import hashlib
import importlib
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Column, DateTime, MetaData, String, Table, func, select

LOCK_SQL = "hashtextextended('freemanone:schema:' || current_schema(), 0)"
logger = logging.getLogger(__name__)
LEDGER = Table(
    "schema_migrations",
    MetaData(),
    Column("version", String(64), primary_key=True),
    Column("checksum", String(64), nullable=False),
    Column(
        "applied_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
)


@dataclass(frozen=True)
class Migration:
    version: str
    module: str
    postgres_only: bool = False

    @property
    def checksum(self):
        module = importlib.import_module("app.database." + self.module)
        return hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()

    async def apply(self, engine):
        if self.postgres_only and engine.dialect.name != "postgresql":
            return  # SQLite is only an isolated development/test bootstrap.
        await importlib.import_module("app.database." + self.module).upgrade(engine)


# Append new versions; never reorder or edit an already released migration.
MIGRATIONS = (
    Migration("001_proxy_compatibility", "proxy_migration"),
    Migration("002_accounts", "account_migration"),
    Migration("003_client_offers", "client_offers_migration"),
    Migration("004_customer_identity", "identity_migration"),
    Migration("005_base_schema", "schema_bootstrap"),
    Migration("006_vless_creation", "vless_creation_migration"),
    Migration("007_vless_renewal", "vless_renewal_migration"),
    Migration("008_vless_capacity", "vless_capacity_migration", True),
    Migration("009_vless_sync", "vless_sync_migration", True),
    Migration("010_vless_fallback", "vless_fallback_migration", True),
    Migration("011_tariff_identity", "tariff_identity_migration", True),
)


class MigrationError(RuntimeError):
    pass


class MigrationBusy(MigrationError):
    pass


class MigrationFailed(MigrationError):
    def __init__(self, version, error):
        self.version = version
        self.error_type = type(error).__name__
        super().__init__(version + ":" + self.error_type)


class _TransactionalConnection:
    """Legacy migrations may commit; the runner owns the PostgreSQL commit."""

    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    async def commit(self):
        pass

    async def rollback(self):
        raise MigrationError("Migration transaction is owned by the runner")


class _TransactionalEngine:
    """Bind existing begin/connect migration APIs to one fenced transaction."""

    def __init__(self, connection):
        self.dialect = connection.dialect
        self.connection = _TransactionalConnection(connection)

    @asynccontextmanager
    async def begin(self):
        yield self.connection

    connect = begin


def history_issues(rows, migrations=MIGRATIONS):
    expected = {m.version: m.checksum for m in migrations}
    issues = []
    for version, checksum in rows.items():
        if version not in expected:
            issues.append("unknown_migration:" + version)
        elif checksum != expected[version]:
            issues.append("checksum_mismatch:" + version)
    prefix = [m.version for m in migrations][: len(rows)]
    if set(rows) != set(prefix):
        issues.append("non_contiguous_history")
    return issues


@asynccontextmanager
async def sqlite_lock(engine, *, shared=False):
    """Local test files only; PostgreSQL uses session advisory locks."""
    import fcntl

    path = engine.url.database
    if not path or path == ":memory:":
        # In-memory SQLite cannot be shared across independent processes.
        yield
        return
    with Path(path + ".schema.lock").open("a") as file:
        try:
            fcntl.flock(
                file, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
            )
        except BlockingIOError:
            raise MigrationBusy("schema_in_use") from None
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


async def _upgrade_locked(
    engine, connection, migrations, lock_timeout, statement_timeout
):
    async with connection.begin():
        if engine.dialect.name == "postgresql":
            await connection.exec_driver_sql(
                f"SET LOCAL lock_timeout = '{lock_timeout}s'"
            )
            await connection.exec_driver_sql(
                f"SET LOCAL statement_timeout = '{statement_timeout}s'"
            )
        await connection.run_sync(lambda c: LEDGER.create(c, checkfirst=True))
        rows = dict(
            (
                await connection.execute(select(LEDGER.c.version, LEDGER.c.checksum))
            ).all()
        )
    issues = history_issues(rows, migrations)
    if issues:
        raise MigrationError(",".join(issues))
    applied = []
    for migration in migrations:
        if migration.version in rows:
            continue
        logger.info("Migration starting: %s", migration.version)
        try:
            if engine.dialect.name == "postgresql":
                async with connection.begin():
                    await connection.exec_driver_sql(
                        f"SET LOCAL lock_timeout = '{lock_timeout}s'"
                    )
                    await connection.exec_driver_sql(
                        f"SET LOCAL statement_timeout = '{statement_timeout}s'"
                    )
                    await migration.apply(_TransactionalEngine(connection))
                    await connection.execute(
                        LEDGER.insert().values(
                            version=migration.version, checksum=migration.checksum
                        )
                    )
            else:
                # SQLite compatibility rebuilds manage PRAGMAs/commits.
                await migration.apply(engine)
                async with connection.begin():
                    await connection.execute(
                        LEDGER.insert().values(
                            version=migration.version, checksum=migration.checksum
                        )
                    )
        except Exception as error:
            logger.error(
                "Migration failed: %s (%s)", migration.version, type(error).__name__
            )
            raise MigrationFailed(migration.version, error) from None
        logger.info("Migration applied: %s", migration.version)
        applied.append(migration.version)
    return applied


async def upgrade(
    engine, *, migrations=MIGRATIONS, lock_timeout=5, statement_timeout=300
):
    """Fail fast when another runner or a serving application owns the schema."""
    if not (
        isinstance(lock_timeout, int)
        and 1 <= lock_timeout <= 3600
        and isinstance(statement_timeout, int)
        and 1 <= statement_timeout <= 86400
    ):
        raise ValueError("Invalid migration timeout")
    if engine.dialect.name not in {"postgresql", "sqlite"}:
        raise MigrationError("unsupported_database")
    if engine.dialect.name == "sqlite":
        async with sqlite_lock(engine), engine.connect() as connection:
            return await _upgrade_locked(
                engine, connection, migrations, lock_timeout, statement_timeout
            )
    # The lock never outlives the executing connection. Connection death rolls
    # back the current step *and* releases ownership, including on cancellation.
    async with engine.connect() as connection:
        acquired = await connection.exec_driver_sql(
            f"SELECT pg_try_advisory_lock({LOCK_SQL})"
        )
        if not acquired.scalar_one():
            await connection.rollback()
            raise MigrationBusy("schema_in_use")
        await connection.commit()
        try:
            return await _upgrade_locked(
                engine, connection, migrations, lock_timeout, statement_timeout
            )
        finally:
            if not connection.invalidated:
                try:
                    await connection.rollback()
                    await connection.exec_driver_sql(
                        f"SELECT pg_advisory_unlock({LOCK_SQL})"
                    )
                    await connection.commit()
                except BaseException as error:
                    # Never return a session with an uncertain session lock to
                    # the connection pool or hide the original step failure.
                    await connection.invalidate()
                    if not isinstance(error, Exception):
                        raise


async def _command(args):
    from app.config.settings import Settings
    from app.database.schema_readiness import inspect_schema
    from app.database.session import Database

    database = Database(Settings().database_url)
    try:
        applied = []
        if args.command == "upgrade":
            applied = await upgrade(
                database.engine,
                lock_timeout=args.lock_timeout,
                statement_timeout=args.statement_timeout,
            )
        state = await inspect_schema(database.engine)
        print(json.dumps({**state.as_dict(), "applied": applied}, sort_keys=True))
        return 0 if state.ready else 1
    except Exception as error:
        # Never print DSNs, SQL parameters, or database exception contents.
        payload = {"status": "NOT READY", "error": type(error).__name__}
        if isinstance(error, MigrationFailed):
            payload.update(failed_migration=error.version, cause=error.error_type)
        elif isinstance(error, MigrationError):
            payload["reason"] = str(error)
        print(json.dumps(payload))
        return 2
    finally:
        await database.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("upgrade", "check"))
    parser.add_argument("--lock-timeout", type=int, default=5)
    parser.add_argument("--statement-timeout", type=int, default=300)
    raise SystemExit(asyncio.run(_command(parser.parse_args())))


if __name__ == "__main__":
    main()
