"""Website-only confirmed ownership transfer; no provisioning or billing calls."""
import hashlib,json,secrets
from datetime import timezone
from fastapi import APIRouter,Request,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select,or_,func
from app.database.models import User,AccessKey,Subscription,VlessCredential,VpnCredential,Device,Payment,Gift,AdminAudit,now
from app.database.repositories import SQLRepository
from app.services.customer_identity import lock_nonce

PENDING=('created','pending','order_created','order_pending','order_processing','card_created','card_sending','card_uncertain','stars_created','stars_pending','stars_processing','manual_preparing','manual_received_pending')
def utc(value):return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
async def owner(s,settings,actor):
 fresh=await s.get(User,actor.id,populate_existing=True)
 if not fresh or fresh.status!='active' or not settings.is_owner(fresh.telegram_id):raise PermissionError('Передавать подписки может только владелец')
 return fresh
async def bundle(s,key_id):
 key=await s.get(AccessKey,key_id,populate_existing=True)
 if not key or key.status!='active' or utc(key.expires_at)<=now():raise ValueError('Выберите действующую активную подписку')
 vpn=await s.scalar(select(VpnCredential).where(VpnCredential.access_key_id==key.id).execution_options(populate_existing=True))
 vless=await s.scalar(select(VlessCredential).where(VlessCredential.access_key_id==key.id).execution_options(populate_existing=True))
 if bool(vpn)==bool(vless) or key.mode=='mock':raise ValueError('Не найдена однозначная связь с действующим VPN-подключением')
 credential=vless or vpn;sub=await s.get(Subscription,credential.subscription_id,populate_existing=True)
 if not sub or sub.user_id!=key.user_id or credential.user_id!=key.user_id or credential.status!='active' or utc(sub.expires_at)<=now():raise ValueError('Связи подписки изменились. Обновите страницу')
 repo=SQLRepository(s)
 if await repo.setting(f'subscription_status:{sub.id}','active')!='active' or any([await repo.setting(f'subscription_{field}:{sub.id}')=='1' for field in ('removed','transferred')]):raise ValueError('Подписка недоступна для передачи')
 from app.services.recurring_cards import unsettled_card
 if await unsettled_card(repo,sub.id):raise ValueError('Сначала дождитесь завершения текущего списания за подписку')
 from app.database.models import VlessRenewal
 if await s.scalar(select(VlessRenewal.id).where(VlessRenewal.subscription_id==sub.id,VlessRenewal.state.not_in(('applied','failed'))).limit(1)):raise ValueError('Сначала завершите или восстановите текущую операцию с VPN-панелью')
 for pointer,prefix in (('bot_upgrade_open:','bot_upgrade:'),('bot_antig_open:','bot_antig_addon:'),('web_antig_open:','web_key_antig:')):
  token=await repo.setting(pointer+str(key.user_id)+':'+str(sub.id))
  if token:
   try:operation=json.loads(await repo.setting(prefix+token,'{}'))
   except (ValueError,TypeError):raise ValueError('Не удалось проверить текущее улучшение подписки')
   if operation.get('held') and not operation.get('done'):raise ValueError('По этой подписке зарезервирована оплата улучшения. Сначала завершите или отмените её')

 for p in await s.scalars(select(Payment).where(Payment.user_id==key.user_id,Payment.status.in_(PENDING))):
  raw=await repo.setting(f'order:{p.id}','{}')
  try:order=json.loads(raw)
  except (ValueError,TypeError):raise ValueError('Не удалось проверить незавершённый платёж')
  if p.subscription_id==sub.id or order.get('sid')==sub.id:raise ValueError('По этой подписке есть незавершённая оплата. Дождитесь её завершения')
 if await s.scalar(select(Gift.id).where(Gift.key_id==key.id,Gift.status.in_(('draft','ready'))).limit(1)):raise ValueError('Подписка уже участвует в незавершённом подарке')
 return key,sub,credential
async def move_reminders(s,sub_id,source,token):
 from app.database.models import Delivery,EmailDelivery
 # Do not redirect the previous buyer's messages to another person's account.
 for model,extra in ((Delivery,Delivery.campaign_id.is_(None)),(EmailDelivery,EmailDelivery.kind=='expiry')):
  rows=list(await s.scalars(select(model).where(model.subscription_id==sub_id,model.user_id==source,model.status=='pending',extra).with_for_update()))
  for row in rows:row.status='skipped';row.dedup='transfer:'+token+':'+model.__tablename__+':'+str(row.id)
  await s.flush()
  if await s.scalar(select(model.id).where(model.subscription_id==sub_id,model.user_id==source,model.status=='sending',extra).limit(1)):raise ValueError('Уведомление об этой подписке уже отправляется. Повторите передачу через несколько секунд')
async def recipient(s,target_id,source):
 target=await s.get(User,target_id,populate_existing=True)
 if not target or target.status!='active' or not target.freeman_id:raise ValueError('Выберите зарегистрированного активного клиента')
 if target.id==source:raise ValueError('Подписка уже принадлежит этому клиенту')
 return target
async def signature(s,key,sub,c):
 repo=SQLRepository(s)
 values=[key.id,key.user_id,key.status,key.mode,key.name,utc(key.expires_at).isoformat(),sub.id,sub.user_id,sub.kind,sub.tariff_id,sub.device_limit,utc(sub.expires_at).isoformat(),c.id,c.user_id,c.status,utc(c.expires_at).isoformat(),getattr(c,'subscription_url',getattr(c,'config','')),getattr(c,'remote_user_uuid',getattr(c,'remote_client_id',''))]
 for name in ('auto_renew','auto_source','auto_card','auto_renew_cycle'):values.append(await repo.setting(f'{name}:{sub.id}'))
 return hashlib.sha256(json.dumps(values,ensure_ascii=False).encode()).hexdigest()
def person(u):return dict(id=u.id,name=u.display_name,freeman_id=u.freeman_id)
async def preview(s,settings,actor,key_id,target_id):
 await owner(s,settings,actor);key,sub,c=await bundle(s,key_id);target=await recipient(s,target_id,key.user_id);source=await s.get(User,key.user_id)
 token=secrets.token_hex(16);data=dict(actor=actor.id,key=key.id,source=key.user_id,target=target.id,signature=await signature(s,key,sub,c),expires=now().timestamp()+300)
 await SQLRepository(s).set_setting('web_subscription_transfer:'+token,json.dumps(data))
 return dict(token=token,key=dict(id=key.id,name=key.name,expires_at=utc(key.expires_at).isoformat(),device_limit=sub.device_limit),source=person(source),target=person(target),devices=await s.scalar(select(func.count()).select_from(Device).where(Device.subscription_id==sub.id,Device.status!='deleted')),auto_renew_disabled=True)
async def confirm(s,settings,actor,token):
 await owner(s,settings,actor);repo=SQLRepository(s);name='web_subscription_transfer:'+token
 raw=await repo.setting(name,'{}');data=json.loads(raw)
 if data.get('actor')!=actor.id:raise PermissionError('Подтверждение недоступно')
 # Match the existing owner action lock, then serialise all quotes for this key.
 await s.scalar(select(User).where(User.id==actor.id).with_for_update())
 await lock_nonce(s,'web-subscription-transfer:'+str(data['key']))
 raw=await repo.setting(name,'{}');data=json.loads(raw)
 if data.get('done'):return data['result']
 if data.get('expires',0)<now().timestamp():raise ValueError('Подтверждение устарело. Выберите клиента заново')
 for uid in sorted({data['source'],data['target']}-{actor.id}):await s.scalar(select(User).where(User.id==uid).with_for_update())
 await s.scalar(select(AccessKey).where(AccessKey.id==data['key']).with_for_update())
 key,sub,c=await bundle(s,data['key']);target=await recipient(s,data['target'],key.user_id)
 if key.user_id!=data['source'] or await signature(s,key,sub,c)!=data['signature']:raise ValueError('Подписка изменилась после предпросмотра. Проверьте передачу заново')
 await move_reminders(s,sub.id,data['source'],token)
 key.user_id=target.id;sub.user_id=target.id;c.user_id=target.id
 for field,value in (('auto_renew','0'),('auto_card',''),('auto_source','balance'),('auto_renew_cycle','monthly')):await repo.set_setting(f'{field}:{sub.id}',value)
 # Included Proxy is account-level. Reconcile entitlements instead of moving a
 # shared/account-owned Proxy or an independently purchased connection.
 from app.services.proxy_control import queue_included
 await queue_included(repo,sub)
 result=dict(ok=True,key_id=key.id,subscription_id=sub.id,source_id=data['source'],target_id=target.id)
 data.update(done=True,result=result);await repo.set_setting(name,json.dumps(data))
 s.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='web_subscription_transfer',target=str(key.id),details=json.dumps(dict(source=data['source'],target=target.id,subscription=sub.id,expiry=utc(key.expires_at).isoformat(),auto_renew_disabled=True))))
 await s.flush();return result
class Choice(BaseModel):
 key_id:int=Field(gt=0,strict=True)
 target_id:int=Field(gt=0,strict=True)
class Confirmation(BaseModel):
 token:str=Field(pattern=r'^[a-f0-9]{32}$')
def transfer_router(database,settings,current_user):
 router=APIRouter()
 async def actor(request):
  user=await current_user(request.cookies.get('freeman_session'))
  if not settings.is_owner(user.telegram_id):raise HTTPException(403,'Недостаточно прав')
  if request.method!='GET' and (request.headers.get('sec-fetch-site')=='cross-site' or request.headers.get('origin') not in (None,settings.web_base_url.rstrip('/'))):raise HTTPException(403,'Неверный источник запроса')
  return user
 @router.get('/api/web/admin/key-transfer/clients')
 async def clients(request:Request,q:str='',exclude:int=0):
  user=await actor(request);q=q.strip().lstrip('@')
  if len(q)<2:return {'items':[]}
  term='%'+q[:100].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
  async with database.sessions() as s:
   await owner(s,settings,user)
   from app.services.accounts import freeman_id_aliases
   rows=await s.scalars(select(User).where(User.id!=exclude,User.status=='active',User.freeman_id.is_not(None),or_(User.first_name.ilike(term,escape='\\'),User.email.ilike(term,escape='\\'),User.username.ilike(term,escape='\\'),User.freeman_id.in_(freeman_id_aliases(q)),User.freeman_id.ilike(term,escape='\\'))).order_by(User.id.desc()).limit(15))
   return {'items':[person(u) for u in rows]}
 @router.post('/api/web/admin/key-transfer/preview')
 async def proposed(payload:Choice,request:Request):
  user=await actor(request)
  try:
   async with database.sessions.begin() as s:return await preview(s,settings,user,payload.key_id,payload.target_id)
  except PermissionError as e:raise HTTPException(403,str(e)) from None
  except ValueError as e:raise HTTPException(409,str(e)) from None
 @router.post('/api/web/admin/key-transfer/confirm')
 async def applied(payload:Confirmation,request:Request):
  user=await actor(request)
  try:
   async with database.sessions.begin() as s:return await confirm(s,settings,user,payload.token)
  except PermissionError as e:raise HTTPException(403,str(e)) from None
  except ValueError as e:raise HTTPException(409,str(e)) from None
 return router
