"""Shared customer account controls; no separate identity or billing ledger."""
import asyncio,hashlib,json
from datetime import timedelta,timezone
from pathlib import Path
from cryptography.fernet import Fernet,InvalidToken
from fastapi import APIRouter,Request,HTTPException
from fastapi.routing import APIRoute
from fastapi.responses import FileResponse,JSONResponse,RedirectResponse
from pydantic import BaseModel,Field
from sqlalchemy import select,update,func
from sqlalchemy.exc import IntegrityError
from app.database.models import User,AccountChallenge,AppSetting,Payment,Delivery,now
from app.database.repositories import SQLRepository
class PasswordSetup(BaseModel):
 token:str=Field(min_length=20,max_length=1000)
 password:str=Field(min_length=10,max_length=128)
class Bind(BaseModel):
 nonce:str=Field(min_length=32,max_length=100)
class Renewal(BaseModel):
 sid:int
 enabled:bool
 source:str=Field(pattern="^(balance|card)$")
 method:str=Field(default="",max_length=100)
 months:int|None=Field(default=None,ge=1,le=36)
 tariff_id:int|None=Field(default=None,ge=1)
class Remove(BaseModel):
 method:str=Field(min_length=1,max_length=100)
class Toggle(BaseModel):
 enabled:bool
class ProviderObject(BaseModel):
 id:str=Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')
class ProviderNotification(BaseModel):
 event:str=Field(pattern=r'^payment\.(succeeded|canceled)$')
 object:ProviderObject

def account_notice(value, reminder=False):
 # This historical namespace also contains ISO timestamps with |read/|hidden.
 # Only complete notice payloads belong in the account reminder list.
 try:data=json.loads(value)
 except (ValueError,TypeError):return None
 if not isinstance(data,dict) or type(data.get('subscription_id')) is not int or data['subscription_id']<=0 or not isinstance(data.get('text'),str):return None
 if reminder and not isinstance(data.get('expiry'),str):return None
 return data

def management_router(database,settings,current_user,session_token,cookie):
 class PrivateRoute(APIRoute):
  def get_route_handler(self):
   handler=super().get_route_handler()
   async def private(request):
    response=await handler(request);response.headers['Cache-Control']='no-store';response.headers['Referrer-Policy']='no-referrer';return response
   return private
 router=APIRouter(route_class=PrivateRoute)
 def origin(request):
  if (request.headers.get('origin') and request.headers['origin']!=settings.web_base_url) or request.headers.get('sec-fetch-site')=='cross-site':raise HTTPException(403,'Откройте страницу на сайте Freeman ONE.')
 @router.get('/account/card-link')
 async def telegram_card_link(token:str=''):
  from app.services.web_login import consume_direct
  from app.services.recurring_cards import start_bind
  async with database.sessions.begin() as session:
   result=await consume_direct(session,settings,token,'card_bind')
  if not result:raise HTTPException(401,'Ссылка истекла. Получите новую в боте.')
  async with database.sessions() as session:user=await session.get(User,result[0])
  try:payment=await start_bind(database,settings,user,hashlib.sha256(token.encode()).hexdigest())
  except ValueError as error:raise HTTPException(422,str(error))
  response=RedirectResponse(payment['url'],status_code=303)
  response.set_cookie(cookie,session_token(*result),httponly=True,secure=True,samesite='lax',path='/')
  return response
 @router.get('/account')
 async def page(login:str=''):
  if login:
   from app.services.web_login import consume_direct
   async with database.sessions.begin() as s:identity=await consume_direct(s,settings,login)
   if not identity:return RedirectResponse('/?auth=expired',303)
   r=RedirectResponse('/account',303);r.set_cookie(cookie,session_token(*identity),httponly=True,secure=True,samesite='lax',max_age=86400*30);r.headers['Cache-Control']='no-store';r.headers['Referrer-Policy']='no-referrer';return r
  return FileResponse(Path(__file__).with_name('static')/'account-management.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
 @router.post('/api/payments/yookassa/webhook')
 async def payment_notification(payload:ProviderNotification):
  # A public notification is only a hint: authenticate every detail via the provider API.
  from app.services.topups import YooKassa,TopupService,SubscriptionCheckoutService
  if not settings.yookassa_shop_id or not settings.yookassa_secret_key:raise HTTPException(503,'Оплата недоступна.')
  try:data=await YooKassa(settings).get(payload.object.id)
  except ValueError:raise HTTPException(503,'Повторите уведомление позже.')
  try:pid=int(data.get('metadata',{}).get('freeman_payment',''))
  except (ValueError,TypeError):return {'ok':True}
  async with database.sessions.begin() as s:
   repo=SQLRepository(s);p=await s.scalar(select(Payment).where(Payment.id==pid).with_for_update())
   if not p:return {'ok':True}
   if p.provider_id and p.provider_id!=payload.object.id:raise HTTPException(422,'Платёж не совпадает.')
   if not p.provider_id:
    attempt=await repo.setting(f'card_attempt:{p.id}')
    guest=await repo.setting(f'guest_payment:{p.id}')
    if not guest and (not attempt or not json.loads(attempt).get('started_at')):raise HTTPException(422,'Платёж не ожидается.')
    if guest and p.status!='order_created':raise HTTPException(422,'Платёж не ожидается.')
    order=await SubscriptionCheckoutService(repo,settings).details(p)
    from decimal import Decimal
    meta=data.get('metadata',{});amount=data.get('amount',{})
    if data.get('id')!=payload.object.id or meta.get('freeman_user')!=str(p.user_id) or meta.get('freeman_kind')!='subscription' or meta.get('freeman_sid')!=str(order['sid']) or meta.get('freeman_months')!=str(order['months']) or amount.get('currency')!='RUB' or Decimal(amount.get('value','0'))*100!=p.amount or bool(data.get('test',False))!=settings.yookassa_test_mode:raise HTTPException(422,'Данные платежа не совпадают.')
    p.provider_id=payload.object.id;p.status='order_pending'
   try:
    if p.status.startswith('topup_'):await TopupService(repo,settings).credit(p,data)
    elif p.status.startswith('order_'):await SubscriptionCheckoutService(repo,settings).credit(p,data)
    elif p.status.startswith('proxy_order_'):
     from app.services.proxies import ProxyCheckoutService
     await ProxyCheckoutService(repo,settings).credit(p,data)
   except ValueError:raise HTTPException(503,'Повторите подтверждение позже.')
  return {'ok':True}
 @router.post('/api/account/password/start')
 async def password_start(request:Request):
  origin(request)
  actor=await current_user(request.cookies.get(cookie))
  if actor.password_hash:raise HTTPException(409,'Пароль уже установлен. Введите текущий пароль.')
  from app.services.accounts import throttle
  async with database.sessions.begin() as s:
   try:await throttle(s,'password-first',str(actor.id),5,600)
   except ValueError:raise HTTPException(429,'Слишком много попыток. Повторите позже.')
  from app.services.web_login import issue_direct
  return {'token':issue_direct(settings,actor,'password_setup_first'),'expires_in':600}
 @router.post('/api/account/password/setup')
 async def setup(payload:PasswordSetup,request:Request):
  origin(request)
  try:
   data=json.loads(Fernet(settings.encryption_key.encode()).decrypt(payload.token.encode(),ttl=600))
   purpose=data.get('purpose')
   if purpose not in ('password_setup','password_setup_first') or not isinstance(data.get('nonce'),str):raise ValueError()
   uid=int(data['user_id']);version=int(data['version'])
  except (InvalidToken,ValueError,KeyError,TypeError):raise HTTPException(422,'Ссылка истекла. Получите новую в Telegram-боте.')
  if purpose=='password_setup_first':
   actor=await current_user(request.cookies.get(cookie))
   if actor.id!=uid or actor.auth_version!=version:raise HTTPException(403,'Подтвердите вход в этот аккаунт.')
  from app.services.accounts import throttle
  async with database.sessions.begin() as check:
   existing=await check.scalar(select(AccountChallenge.id).where(AccountChallenge.token_hash==hashlib.sha256(payload.token.encode()).hexdigest()))
   if existing:raise HTTPException(422,'Ссылка уже использована.')
   try:await throttle(check,'password-setup',str(uid),10)
   except ValueError:raise HTTPException(429,'Слишком много попыток. Повторите позже.')
  from app.web.guest_purchase import password_hash
  hashed=await asyncio.to_thread(password_hash,payload.password)
  try:
   async with database.sessions.begin() as s:
    user=await s.scalar(select(User).where(User.id==uid).with_for_update())
    if not user or user.status!='active' or user.auth_version!=version:raise HTTPException(422,'Ссылка недоступна.')
    if purpose=='password_setup_first':
     if user.password_hash:raise HTTPException(409,'Пароль уже установлен. Введите текущий пароль.')
    elif not user.telegram_id:raise HTTPException(422,'Ссылка недоступна.')
    s.add(AccountChallenge(token_hash=hashlib.sha256(payload.token.encode()).hexdigest(),purpose='password_setup',user_id=uid,consumed=True,expires_at=now()+timedelta(minutes=10)))
    await s.flush();user.password_hash=hashed;user.auth_version+=1;version=user.auth_version
  except IntegrityError:raise HTTPException(422,'Ссылка уже использована.')
  r=JSONResponse({'ok':True});r.set_cookie(cookie,session_token(uid,version),httponly=True,secure=True,samesite='lax',max_age=86400*30);return r
 @router.get('/api/account/management')
 async def details(request:Request):
  user=await current_user(request.cookies.get(cookie))
  async with database.sessions() as s:
   repo=SQLRepository(s);subs=[]
   for sub in await repo.subscriptions(user.id):
    if await repo.setting(f'subscription_removed:{sub.id}')=='1' or await repo.setting(f'subscription_transferred:{sub.id}')=='1':continue
    from app.services.screens import ScreenService
    name=await ScreenService(repo,settings,None,None).subscription_list_label(sub)
    from app.services.recurring_cards import renewal_status
    renewal=await renewal_status(repo,settings,user,sub);enabled=renewal['enabled']
    selected=await repo.auto_tariff(sub.id)
    subs.append(dict(id=sub.id,name=name,tariff_id=selected.id if selected else None,devices=sub.device_limit,months=int(await repo.setting(f'auto_renew_months:{sub.id}','1')),source=await repo.setting(f'auto_source:{sub.id}','balance'),method=await repo.setting(f'auto_card:{sub.id}',''),tariffs=[dict(id=t.id,months=t.months,devices=t.device_limit,price=(await __import__('app.services.pricing',fromlist=['quote']).quote(repo,user.id,t,sub.id))[0]) for t in await repo.tariffs() if t.device_limit==sub.device_limit and await repo.tariff_setting(t, 'tariff_enabled','1')=='1'],expires_at=sub.expires_at.isoformat(),enabled=enabled,effective=renewal['effective'],reason=renewal['reason']))
   attempts=[]
   for p in await s.scalars(select(Payment).where(Payment.user_id==user.id).order_by(Payment.id.desc()).limit(100)):
    value=await repo.setting(f'card_attempt:{p.id}')
    if value:
     job=json.loads(value);attempts.append(dict(id=p.id,sid=job['sid'],amount=p.amount,status=p.status,created_at=p.created_at.isoformat()))
   return dict(is_admin=settings.is_owner(user.telegram_id),attempts=attempts,subscriptions=subs,master=await repo.setting(f'auto_master:{user.id}','1')=='1',trial_available=not user.trial_used and await repo.setting('trial_enabled','1')=='1',trial_hours=int(await repo.setting('trial_hours','24')),password_set=bool(user.password_hash),payment_methods=await __import__("app.services.recurring_cards",fromlist=["methods"]).methods(repo,user.id),tariffs=[dict(id=t.id,months=t.months,devices=t.device_limit,price=(await __import__('app.services.pricing',fromlist=['quote']).quote(repo,user.id,t))[0]) for t in await repo.tariffs() if await repo.tariff_setting(t, "tariff_enabled","1")=="1"])
 @router.get('/api/admin/renewal-status')
 async def admin_status(request:Request):
  actor=await current_user(request.cookies.get(cookie))
  if not settings.is_owner(actor.telegram_id):raise HTTPException(403,'Требуется доступ администратора.')
  async with database.sessions() as s:
   repo=SQLRepository(s);items=[]
   for row in await s.scalars(select(AppSetting).where(AppSetting.key.startswith('card_attempt:')).order_by(AppSetting.key.desc()).limit(100)):
    job=json.loads(row.value);payment=await s.get(Payment,int(row.key.split(':')[1]))
    if payment:items.append(dict(payment_id=payment.id,user_id=payment.user_id,subscription_id=job['sid'],status=payment.status,amount=payment.amount,attempts=job['attempts'],next_at=job.get('next_at')))
   return dict(items=items,trial_used=await s.scalar(select(func.count(User.id)).where(User.trial_used==True)),reminders=[dict(id=d.id,sid=d.subscription_id,status=d.status,attempts=d.attempts) for d in await s.scalars(select(Delivery).where(Delivery.subscription_id.is_not(None)).order_by(Delivery.id.desc()).limit(50))],trial_enabled=await repo.setting('trial_enabled','1')=='1',trial_hours=int(await repo.setting('trial_hours','24')),reminders_enabled=await repo.setting('reminders_enabled','1')=='1',reminder_hours=int(await repo.setting('reminder_hours','72')))
 @router.post('/api/account/cards/bind')
 async def bind(payload:Bind,request:Request):
  origin(request);user=await current_user(request.cookies.get(cookie))
  from app.services.recurring_cards import start_bind
  try:return await start_bind(database,settings,user,payload.nonce)
  except ValueError as e:raise HTTPException(422,str(e))
 @router.post('/api/account/cards/remove')
 async def remove(payload:Remove,request:Request):
  origin(request);user=await current_user(request.cookies.get(cookie))
  async with database.sessions.begin() as s:
   repo=SQLRepository(s);key=f'card:{user.id}:{payload.method}';value=await repo.setting(key)
   if not value:raise HTTPException(404,'Карта не найдена.')
   m=json.loads(value);m['active']=False;await repo.set_setting(key,json.dumps(m))
   for sub in await repo.subscriptions(user.id):
    if await repo.setting(f'auto_card:{sub.id}')==payload.method:await repo.set_setting(f'auto_renew:{sub.id}','0')
  return {'ok':True}
 @router.post('/api/account/renewal/subscription')
 async def renewal(payload:Renewal,request:Request):
  origin(request);user=await current_user(request.cookies.get(cookie))
  async with database.sessions.begin() as s:
   await s.execute(update(User).where(User.id==user.id).values(status=User.status));repo=SQLRepository(s)
   from app.services.billing import BillingService
   try:
    if payload.enabled:sub,_=await BillingService(repo,settings).valid(user,payload.sid)
    else:
     sub=await repo.subscription(user.id,payload.sid)
     if not sub:raise ValueError('Подписка недоступна.')
   except ValueError as e:raise HTTPException(422,str(e))
   tariff=await repo.select_tariff(payload.months,payload.tariff_id)
   if payload.enabled and (not tariff or tariff.device_limit!=sub.device_limit or await repo.tariff_setting(tariff, 'tariff_enabled','1')!='1'):raise HTTPException(422,'Тариф недоступен для этой подписки.')
   if payload.enabled and payload.source=='card':
    from app.services.recurring_cards import methods
    if not any(m['id']==payload.method for m in await methods(repo,user.id)):raise HTTPException(422,'Сначала привяжите карту.')
   if payload.enabled and payload.source=='card':
    cycle=f'card_cycle:{sub.id}:'+hashlib.sha256(sub.expires_at.replace(tzinfo=timezone.utc).isoformat().encode()).hexdigest()
    previous=await repo.setting(cycle)
    if previous:
     last=await s.get(Payment,int(previous))
     if last and last.status=='order_canceled':await repo.set_setting(cycle,'')
   await repo.set_setting(f'auto_renew:{sub.id}','1' if payload.enabled else '0')
   if tariff:
    await repo.set_setting(f'auto_renew_months:{sub.id}',str(tariff.months))
    await repo.set_setting(f'auto_renew_tariff:{sub.id}',str(tariff.id))
   await repo.set_setting(f'auto_source:{sub.id}',payload.source);await repo.set_setting(f'auto_card:{sub.id}',payload.method)
   await repo.set_setting(f'auto_renew_cycle:{sub.id}','monthly')
  return {'ok':True}
 @router.post('/api/account/renewal/master')
 async def master(payload:Toggle,request:Request):
  origin(request);user=await current_user(request.cookies.get(cookie))
  async with database.sessions.begin() as s:await SQLRepository(s).set_setting(f'auto_master:{user.id}','1' if payload.enabled else '0')
  return {'ok':True}
 @router.post('/api/account/trial')
 async def trial(request:Request):
  origin(request);user=await current_user(request.cookies.get(cookie))
  async with database.sessions.begin() as s:
   repo=SQLRepository(s)
   from app.services.domain import SubscriptionService
   from app.services.vless_provision import ensure
   sub=await SubscriptionService(repo,settings.device_limit).activate_trial(user.id)
   if not sub:raise HTTPException(409,'Пробный период недоступен или уже использован.')
   await ensure(repo,settings,sub,user,operation_key=f"trial:{user.id}")
   from app.database.models import VlessCredential
   c=await s.scalar(select(VlessCredential).where(VlessCredential.subscription_id==sub.id,VlessCredential.status=='active'))
   if not c:raise HTTPException(503,'Не удалось выпустить рабочий ключ. Попробуйте позже.')
   return {'ok':True,'subscription_id':sub.id}
 @router.get('/api/account/notifications')
 async def notifications(request:Request):
  user=await current_user(request.cookies.get(cookie))
  async with database.sessions() as s:
   repo=SQLRepository(s);items=[]
   for row in await s.scalars(select(AppSetting).where(AppSetting.key.startswith(f'web_notice:{user.id}:')).order_by(AppSetting.key.desc()).limit(100)):
    data=account_notice(row.value,reminder=True)
    if data is None:continue
    sub=await repo.subscription(user.id,data['subscription_id'])
    if await repo.setting('reminders_enabled','1')!='1' or await repo.setting(f'notifications:{user.id}','1')!='1':return {'items':[]}
    if sub and await repo.setting(f'subscription_removed:{sub.id}')!='1' and await repo.setting(f'subscription_transferred:{sub.id}')!='1' and sub.expires_at.replace(tzinfo=timezone.utc).isoformat()==data['expiry'] and sub.expires_at.replace(tzinfo=timezone.utc)>now() and await repo.setting(f'subscription_status:{sub.id}','active')=='active':items.append(data)
   for row in await s.scalars(select(AppSetting).where(AppSetting.key.startswith(f'web_payment_notice:{user.id}:')).limit(100)):
    data=account_notice(row.value)
    if data is not None and await repo.subscription(user.id,data['subscription_id']):items.append(data)
   return {'items':items}
 return router
