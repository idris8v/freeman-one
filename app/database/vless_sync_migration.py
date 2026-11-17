"""Add I04 observation/dispatch tables only; keys and paid terms are untouched.

Prepare/test explicitly before an authorized release. No production invocation.
Existing credentials and unresolved I01 operations are queued incrementally by
workers, avoiding a full-table backfill during the migration.
"""

from app.database.models import VlessSync, VlessSyncGate


async def upgrade(engine):
    if engine.dialect.name != "postgresql":
        raise ValueError("VLESS reconciliation requires PostgreSQL")
    async with engine.begin() as connection:
        for model in (VlessSync, VlessSyncGate):
            await connection.run_sync(
                lambda sync, model=model: model.__table__.create(sync, checkfirst=True)
            )
