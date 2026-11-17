"""Shared active subscription counter, using the existing key status rules."""
from datetime import timezone
from sqlalchemy import select
from app.database.models import AccessKey,VlessCredential,VpnCredential,now
from app.services.client_registry import active_key


async def subscription_active(repo,sub):
    if sub.expires_at.replace(tzinfo=timezone.utc)<=now():return False
    if await repo.setting(f'subscription_status:{sub.id}','active')!='active':return False
    if await repo.setting(f'subscription_removed:{sub.id}')=='1' or await repo.setting(f'subscription_transferred:{sub.id}')=='1':return False
    kid=await repo.setting(f'subscription_key:{sub.id}')
    if not kid:
        kid=await repo.session.scalar(select(VlessCredential.access_key_id).where(VlessCredential.subscription_id==sub.id))
    if not kid:
        kid=await repo.session.scalar(select(VpnCredential.access_key_id).where(VpnCredential.subscription_id==sub.id))
    if not kid:return False
    return bool(await repo.session.scalar(select(AccessKey.id).where(AccessKey.id==int(kid),AccessKey.user_id==sub.user_id,active_key())))
