"""Prepare I03 capacity bookkeeping, preserving existing customers and keys.

Run explicitly before an authorized release; never against production in tests.
"""

from sqlalchemy import inspect, text

from app.database.models import VlessCreation, VlessReservation


async def upgrade(engine):
    if engine.dialect.name != "postgresql":
        raise ValueError("VLESS capacity migration requires PostgreSQL")
    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            "ALTER TABLE remnawave_servers ADD COLUMN IF NOT EXISTS "
            "capacity_revision BIGINT NOT NULL DEFAULT 0"
        )
        await connection.run_sync(
            lambda sync: VlessReservation.__table__.create(sync, checkfirst=True)
        )
        if not await connection.run_sync(
            lambda sync: inspect(sync).has_table(VlessCreation.__tablename__)
        ):
            return
        # Known existing associations are already included in users_total.
        # Unknown legacy creates must retain capacity, even without a local key.
        await connection.execute(
            text("""
            INSERT INTO vless_reservations
                (operation_id, server_id, state, lease_token, lease_until,
                 attempts, updated_at)
            SELECT id, server_id,
                CASE WHEN credential_id IS NOT NULL THEN 'consumed'
                     WHEN state = 'failed' THEN 'released' ELSE 'reserved' END,
                '', NULL, 1, CURRENT_TIMESTAMP
            FROM vless_creations
            ON CONFLICT (operation_id) DO NOTHING
        """)
        )
