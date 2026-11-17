"""Explicit base-schema bootstrap; never called by the application lifespan."""

from app.database.models import Base


async def upgrade(engine):
    if engine.dialect.name == "sqlite":
        async with engine.connect() as connection:
            await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            await connection.commit()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        if engine.dialect.name == "postgresql":
            await connection.exec_driver_sql(
                "ALTER TABLE email_deliveries ALTER COLUMN subscription_id DROP NOT NULL"
            )
