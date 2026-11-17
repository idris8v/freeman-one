"""Explicit additive I09 journal migration; never run automatically."""

from sqlalchemy import inspect


async def upgrade(engine):
    if engine.dialect.name != "postgresql":
        raise ValueError("VLESS fallback migration requires PostgreSQL")
    async with engine.begin() as connection:
        if not await connection.run_sync(
            lambda sync: inspect(sync).has_table("vless_creations")
        ):
            raise ValueError("Apply the I01 creation journal migration first")
        # Existing attempts have no durable dispatch provenance or placement
        # policy. Keep them pinned and uncertain, never infer permission to move.
        await connection.exec_driver_sql(
            "ALTER TABLE vless_creations ADD COLUMN IF NOT EXISTS "
            "dispatch_state VARCHAR(24) NOT NULL DEFAULT 'legacy_unknown'"
        )
        await connection.exec_driver_sql(
            "ALTER TABLE vless_creations ADD COLUMN IF NOT EXISTS "
            "placement TEXT NOT NULL DEFAULT '{}'"
        )
