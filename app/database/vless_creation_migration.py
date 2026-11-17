"""Add only the durable VLESS creation journal; no existing rows are changed."""

from app.database.models import VlessCreation


async def upgrade(engine):
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: VlessCreation.__table__.create(sync, checkfirst=True)
        )
