"""Add only the VLESS renewal journal and its pending-operation fence."""

from app.database.models import VlessRenewal


async def upgrade(engine):
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: VlessRenewal.__table__.create(sync, checkfirst=True)
        )
