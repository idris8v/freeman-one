"""Durable customer-scoped service controls. Account and service states are explicit."""
import json,asyncio,logging
from datetime import timedelta
from sqlalchemy import select
from app.database.models import *
from app.database.repositories import SQLRepository
from app.services.offers import utc
async def enqueue(repo,settings,user,actor,action,reason):
 if action=='block':user.status='blocked';user.auth_version+=1
 elif action=='unblock':user.status='active'
 jobs=0
 keys=list(await repo.session.scalars(select(AccessKey).where(AccessKey.user_id==user.id,AccessKey.status!='deleted')))
 for key in keys:
  vc=await repo.session.scalar(select(VlessCredential).where(VlessCredential.access_key_id==key.id));vpn=await repo.session.scalar(select(VpnCredential).where(VpnCredential.access_key_id==key.id));c=vc or vpn
  if not c:continue
  bookmark=f'customer_block_key:{user.id}:{key.id}';saved=await repo.setting(bookmark)
  if action=='unblock' and not saved:continue
  if action=='block' and key.status!='active' and not saved:continue
  if action=='reset_devices' and not vc:continue
  if action=='block':
   if not saved:await repo.set_setting(bookmark,json.dumps(dict(key_status=key.status,credential_status=c.status,subscription_id=c.subscription_id)))
   key.status='billing_blocked';await repo.set_setting(f'subscription_status:{c.subscription_id}','disabled')
  if action=='unblock' and utc(key.expires_at)<=now():await repo.set_setting(bookmark,'');continue
  await repo.set_setting(f'customer_job:{user.id}:key:{key.id}',json.dumps(dict(uid=user.id,kind='key',id=key.id,subscription_id=c.subscription_id,action=action,status='pending',attempts=0,actor=actor.telegram_id,reason=reason,next_at=now().isoformat())));jobs+=1
 if action!='reset_devices':
  for c in await repo.session.scalars(select(ProxyCredential).where(ProxyCredential.user_id==user.id,ProxyCredential.status.not_in(('deleted','migrated')))):
   bookmark=f'customer_block_proxy:{user.id}:{c.id}';saved=await repo.setting(bookmark)
   if action=='unblock' and not saved:continue
   if action=='block' and c.status!='active' and not saved:continue
   if action=='block':
    if not saved:await repo.set_setting(bookmark,json.dumps(dict(status=c.status)))
    c.status='disabled'
   if action=='unblock' and utc(c.expires_at)<=now():await repo.set_setting(bookmark,'');continue
   await repo.set_setting(f'customer_job:{user.id}:proxy:{c.id}',json.dumps(dict(uid=user.id,kind='proxy',id=c.id,action=action,status='pending',attempts=0,actor=actor.telegram_id,reason=reason,next_at=now().isoformat())));jobs+=1
 return dict(ok=True,queued=jobs,status=user.status,note='Изменения сервисов поставлены в надёжную очередь' if jobs else 'Нет подходящих VPN-подписок Remnawave для сброса устройств' if action=='reset_devices' else 'Состояние аккаунта обновлено')
async def process_once(database,settings):
 async with database.sessions.begin() as s:
  repo=SQLRepository(s)
  from sqlalchemy.dialects.postgresql import JSONB
  from sqlalchemy import cast
  rows=await s.scalars(select(AppSetting).where(AppSetting.key.startswith('customer_job:'),cast(AppSetting.value,JSONB)['status'].astext.in_(('pending','error')),cast(AppSetting.value,JSONB)['next_at'].astext<=now().isoformat()).order_by(AppSetting.key).with_for_update(skip_locked=True).limit(100))
  for row in rows:
   v=json.loads(row.value)
   if v['status'] not in ('pending','error') or v.get('next_at','')>now().isoformat():continue
   u=await s.get(User,v['uid'])
   # An older opposite action must not override the latest account state.
   if not u or v['action']=='block' and u.status!='blocked' or v['action']=='unblock' and (u.status=='blocked' or not await repo.setting(f'customer_block_{v["kind"]}:{u.id}:{v["id"]}')):v['status']='superseded';row.value=json.dumps(v);continue
   try:
    if v['kind']=='proxy':
     from app.services.proxies import ProxyService
     c=await s.get(ProxyCredential,v['id']);service=ProxyService(repo,settings)
     if not c or c.status in ('deleted','migrated'):
      v['status']='superseded';row.value=json.dumps(v);continue
     api=service.api(await service.server(c.server_id))
     if v['action']=='block':await api.disable_user(c.remote_username);c.status='disabled'
     else:
      if utc(c.expires_at)<=now():raise ValueError('Срок Proxy истёк')
      await api.enable_user(c.remote_username);c.status='active';await repo.set_setting(f'customer_block_proxy:{u.id}:{c.id}','')
    else:
     key=await s.get(AccessKey,v['id'])
     if not key or key.status=='deleted':
      v['status']='superseded';row.value=json.dumps(v);continue
     vc=await s.scalar(select(VlessCredential).where(VlessCredential.access_key_id==key.id))
     if v['action']=='reset_devices':
      from app.services.remnawave import RemnawaveService
      server=await s.get(RemnawaveServer,vc.server_id);await RemnawaveService(repo,settings).api(server).delete_all_devices(vc.remote_user_uuid)
      await s.execute(__import__('sqlalchemy').update(Device).where(Device.subscription_id==vc.subscription_id).values(status='removed'))
     else:
      if v['action']=='unblock' and utc(key.expires_at)<=now():raise ValueError('Срок ключа истёк')
      from app.services.vpn import VpnService
      await VpnService(repo,settings).set_subscription_status(v['subscription_id'],'disabled' if v['action']=='block' else 'active')
      key.status='billing_blocked' if v['action']=='block' else 'active'
      if vc:vc.status='disabled' if v['action']=='block' else 'active'
      await repo.set_setting(f'subscription_status:{v["subscription_id"]}','disabled' if v['action']=='block' else 'active')
      if v['action']=='unblock':await repo.set_setting(f'customer_block_key:{u.id}:{key.id}','')
    v['status']='done';v['error']=''
    await repo.add(AdminAudit(actor_telegram_id=v['actor'] or 0,action='customer_service_'+v['action'],target=str(u.id),details=json.dumps(dict(service=v['kind'],id=v['id'],result='success',reason=v['reason']))))
   except Exception as e:
    v['status']='error';v['attempts']+=1;v['error']='Провайдер не подтвердил операцию; повторная попытка запланирована';v['next_at']=(now()+timedelta(seconds=min(900,30*v['attempts']))).isoformat()
    await repo.add(AdminAudit(actor_telegram_id=v['actor'] or 0,action='customer_service_'+v['action'],target=str(u.id),details=json.dumps(dict(service=v['kind'],id=v['id'],result='failure',error=type(e).__name__))))
   row.value=json.dumps(v);return True
 return False

async def worker(database,settings):
 while True:
  try:worked=await process_once(database,settings)
  except Exception:
   logging.getLogger(__name__).exception('Customer service control failed');worked=False
  await asyncio.sleep(1 if worked else 3)
