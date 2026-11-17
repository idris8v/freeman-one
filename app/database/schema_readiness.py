"""Read-only schema gate shared by HTTP, Telegram and background startup."""

import asyncio
from dataclasses import dataclass

from sqlalchemy import UniqueConstraint, inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from starlette.responses import JSONResponse

from app.database.migrations import (
    LEDGER,
    LOCK_SQL,
    MIGRATIONS,
    MigrationBusy,
    history_issues,
    sqlite_lock,
)
from app.database.models import Base


@dataclass(frozen=True)
class SchemaState:
    ready: bool
    reason: str = ""
    missing: tuple = ()
    issues: tuple = ()

    def as_dict(self):
        return {
            "status": "READY" if self.ready else "NOT READY",
            "reason": self.reason,
            "required_version": MIGRATIONS[-1].version,
            "missing_migrations": list(self.missing),
            "issues": list(self.issues),
        }


class SchemaNotReady(RuntimeError):
    pass


def _structural_issues(connection):
    inspector = inspect(connection)
    schema = inspector.default_schema_name
    columns = inspector.get_multi_columns(schema=schema)
    issues = []
    for table in Base.metadata.tables.values():
        observed = {c["name"]: c for c in columns.get((schema, table.name), [])}
        if not observed:
            issues.append("missing_table:" + table.name)
        else:
            for name in set(table.columns.keys()) - observed.keys():
                issues.append("missing_column:" + table.name + "." + name)
    if issues:
        return sorted(issues)
    for name, field in (
        ("vless_creations", "id"),
        ("vless_renewals", "id"),
        ("vless_reservations", "operation_id"),
        ("vless_sync", "id"),
        ("vless_sync_gates", "server_id"),
    ):
        if inspector.get_pk_constraint(name)["constrained_columns"] != [field]:
            issues.append("missing_primary_key:" + name)
    # Verify the durable journals' uniqueness fences, not just their columns.
    for name in ("vless_creations", "vless_sync"):
        uniques = {
            tuple(c["column_names"]) for c in inspector.get_unique_constraints(name)
        }
        uniques.update(
            tuple(i["column_names"])
            for i in inspector.get_indexes(name)
            if i["unique"] and not i.get("dialect_options", {}).get("postgresql_where")
        )
        for constraint in Base.metadata.tables[name].constraints:
            if (
                isinstance(constraint, UniqueConstraint)
                and tuple(constraint.columns.keys()) not in uniques
            ):
                issues.append(
                    "missing_unique:" + name + "." + ",".join(constraint.columns.keys())
                )
    index = next(
        (
            i
            for i in inspector.get_indexes("vless_renewals")
            if i["name"] == "uq_vless_renewal_pending_subscription"
        ),
        None,
    )
    if not index or not index["unique"] or index["column_names"] != ["subscription_id"]:
        issues.append("missing_fence:vless_renewals")
    else:
        predicate = index.get("dialect_options", {}).get(
            connection.dialect.name + "_where"
        )
        if predicate is None:
            issues.append("invalid_fence:vless_renewals")
        else:
            # PostgreSQL rewrites NOT IN to <> ALL with type casts. Compare its
            # actual predicate against the expected rule on each journal state.
            if connection.dialect.name == "postgresql":
                states = (
                    "applied",
                    "failed",
                    "prepared",
                    "outcome_unknown",
                    "patch_confirmed",
                    "remote_confirmed",
                    "needs_reconcile",
                    "",
                )
                for state in states:
                    actual = connection.execute(
                        text(
                            f"SELECT ({predicate}) FROM (SELECT CAST(:state AS varchar) AS state) AS candidate"
                        ),
                        {"state": state},
                    ).scalar_one()
                    if actual != (state not in {"applied", "failed"}):
                        issues.append("invalid_fence:vless_renewals")
                        break
            elif str(predicate).strip() != "state NOT IN ('applied', 'failed')":
                issues.append("invalid_fence:vless_renewals")
    for name, field in (
        ("users", "telegram_id"),
        ("guest_orders", "user_id"),
        ("email_deliveries", "subscription_id"),
        ("personal_offers", "key_id"),
        ("personal_offers", "subscription_id"),
    ):
        column = next(c for c in columns[(schema, name)] if c["name"] == field)
        if not column["nullable"]:
            issues.append("invalid_nullability:" + name + "." + field)
    tariffs = inspector.get_unique_constraints("tariffs") + inspector.get_indexes(
        "tariffs"
    )
    if any(
        c["column_names"] == ["months"]
        and c.get("unique", True)
        and not c.get("dialect_options", {}).get("postgresql_where")
        for c in tariffs
    ):
        issues.append("legacy_unique:tariffs.months")
    return sorted(set(issues))


async def _inspect(connection):
    if connection.dialect.name == "postgresql":
        await connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
    if not await connection.run_sync(lambda c: inspect(c).has_table(LEDGER.name)):
        return SchemaState(
            False, "migrations_required", tuple(m.version for m in MIGRATIONS)
        )
    rows = dict(
        (await connection.execute(select(LEDGER.c.version, LEDGER.c.checksum))).all()
    )
    missing = tuple(m.version for m in MIGRATIONS if m.version not in rows)
    issues = history_issues(rows)
    if missing or issues:
        return SchemaState(
            False,
            "migrations_required" if missing else "migration_history_mismatch",
            missing,
            tuple(issues),
        )
    issues = await connection.run_sync(_structural_issues)
    return SchemaState(
        not issues, "schema_mismatch" if issues else "", issues=tuple(issues)
    )


async def inspect_schema(engine):
    """No DDL, backfill or seeding; never trusts SELECT 1 as schema readiness."""
    try:
        async with engine.connect() as connection:
            acquired = False
            if engine.dialect.name == "postgresql":
                acquired = bool(
                    await connection.scalar(
                        text(f"SELECT pg_try_advisory_lock_shared({LOCK_SQL})")
                    )
                )
                await connection.commit()
                if not acquired:
                    return SchemaState(False, "migration_in_progress")
            try:
                return await _inspect(connection)
            finally:
                try:
                    await connection.rollback()
                    if acquired:
                        await connection.exec_driver_sql(
                            f"SELECT pg_advisory_unlock_shared({LOCK_SQL})"
                        )
                        await connection.commit()
                except BaseException:
                    # Session advisory locks must not leak into a pooled session,
                    # even if validation or unlock is interrupted.
                    await connection.invalidate()
                    raise
    except Exception:
        return SchemaState(False, "database_or_schema_unavailable")


class SchemaLease:
    """Serving instances hold a shared session lock; offline DDL is exclusive.

    One dedicated autocommit connection, outside the website/worker pool. A
    broken lease fails closed and requires an application restart, not upgrades.
    """

    def __init__(self, database):
        self.database = database
        self.state = SchemaState(False, "starting")
        self.engine = self.connection = self.local_lock = None
        self.ping_lock = asyncio.Lock()

    async def open(self):
        try:
            return await asyncio.wait_for(self._open(), timeout=10)
        except TimeoutError:
            self.state = SchemaState(False, "schema_check_timeout")
            await self.close()
            return self.state

    async def _open(self):
        try:
            if self.database.engine.dialect.name != "postgresql":
                self.local_lock = sqlite_lock(self.database.engine, shared=True)
                await self.local_lock.__aenter__()
                self.state = await inspect_schema(self.database.engine)
            else:
                async with self.database.engine.connect() as connection:
                    path = await connection.scalar(text("SHOW search_path"))
                self.engine = create_async_engine(
                    self.database.engine.url,
                    poolclass=NullPool,
                    isolation_level="AUTOCOMMIT",
                    hide_parameters=True,
                    connect_args={
                        "timeout": 5,
                        "command_timeout": 5,
                        "server_settings": {
                            "search_path": path,
                            "application_name": "freemanone-schema-owner",
                            "timezone": "UTC",
                        },
                    },
                )
                self.connection = await self.engine.connect()
                acquired = await self.connection.scalar(
                    text(f"SELECT pg_try_advisory_lock_shared({LOCK_SQL})")
                )
                if not acquired:
                    self.state = SchemaState(False, "migration_in_progress")
                else:
                    # _inspect uses SET LOCAL, so validation gets a bounded
                    # transaction; the serving lock itself stays autocommit.
                    await self.connection.rollback()
                    await self.connection.execution_options(
                        isolation_level="READ COMMITTED"
                    )
                    self.state = await _inspect(self.connection)
                    await self.connection.rollback()
                    await self.connection.execution_options(
                        isolation_level="AUTOCOMMIT"
                    )
        except MigrationBusy:
            self.state = SchemaState(False, "migration_in_progress")
        except Exception:
            self.state = SchemaState(False, "database_or_schema_unavailable")
        if not self.state.ready:
            await self.close()
        return self.state

    async def ping(self):
        async with self.ping_lock:
            if not self.state.ready:
                return False
            if self.connection is not None:
                try:
                    await self.connection.exec_driver_sql("SELECT 1")
                except Exception:
                    self.state = SchemaState(False, "schema_lease_lost")
            return self.state.ready

    def require_ready(self):
        if not self.state.ready:
            raise SchemaNotReady(self.state.reason)

    async def run_component(self, factory):
        self.require_ready()
        return await factory()

    async def close(self):
        if self.state.ready:
            self.state = SchemaState(False, "stopped")
        if self.connection is not None:
            await self.connection.close()
            self.connection = None
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
        if self.local_lock is not None:
            lock, self.local_lock = self.local_lock, None
            await lock.__aexit__(None, None, None)


class SchemaReadinessMiddleware:
    def __init__(self, app, database):
        self.app, self.database = app, database

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or path in {"/health", "/live"}
            or path.startswith("/assets/")
        ):
            return await self.app(scope, receive, send)
        lease = getattr(self.database, "schema_lease", None)
        if lease is None or not lease.state.ready:
            response = JSONResponse(
                {
                    "status": "NOT READY",
                    "detail": "Сервис временно недоступен: схема базы данных не готова.",
                },
                status_code=503,
                headers={"Retry-After": "30", "Cache-Control": "no-store"},
            )
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)
