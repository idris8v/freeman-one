"""Audience of shared reply templates. Stored in existing settings; no schema change."""
from sqlalchemy import select
from app.database.models import AppSetting

PREFIX = "web_quick_reply_audience:"

async def customer_ids(session):
    values = await session.scalars(select(AppSetting.key).where(
        AppSetting.key.startswith(PREFIX), AppSetting.value == "customer"))
    return {int(key[len(PREFIX):]) for key in values if key[len(PREFIX):].isdigit()}
