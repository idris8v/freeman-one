from app.web.access import require_business_admin
import json,hashlib
from decimal import Decimal,InvalidOperation
from datetime import datetime,timezone
from fastapi import APIRouter,Cookie,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select,func
from app.database.models import Tariff,PromoCode,PromoActivation,AdminAudit,User
from app.database.repositories import SQLRepository
class Change(BaseModel):
 kind:str=Field(pattern='^(vpn|promotion|code)$')
 id:int=0
 version:int=0
 name:str=Field(min_length=1,max_length=100)
 price:str='0'
 months:int=Field(default=1,ge=1,le=36)
 devices:int=Field(default=1,ge=1,le=100,strict=True)
 traffic_gb:int=Field(default=0,ge=0,le=100000,strict=True)
 enabled:bool=True
 archived:bool=False
 discount_kind:str=Field(default='percent',pattern='^(percent|fixed)$')
 value:str='10'
 product:str=Field(default='vpn',pattern='^(vpn|proxy|all)$')
 target:str=Field(default='all',pattern='^(all|purchase|renewal)$')
 plan_ids:list[int]=Field(default_factory=list,max_length=100)
 starts_at:datetime|None=None
 ends_at:datetime|None=None
 confirmed:bool=False
 request_id:str=Field(min_length=10,max_length=64,pattern='^[a-zA-Z0-9-]+$')
def amount(v):
 try:
  x=Decimal(v.replace(',','.'))*100
  if not x.is_finite() or not 100<=x<=100000000 or x!=x.to_integral_value():raise ValueError()
  return int(x)
 except (ValueError,InvalidOperation):raise HTTPException(422,'Сумма — от 1 до 1 000 000 ₽, с точностью до копейки')
def dates(p):
 for x in (p.starts_at,p.ends_at):
  if x and x.tzinfo is None:raise HTTPException(422,'Укажите часовой пояс даты')
 if p.starts_at and p.ends_at and p.ends_at<=p.starts_at:raise HTTPException(422,'Дата окончания должна быть позже начала')
 return dict(starts_at=p.starts_at.astimezone(timezone.utc).isoformat() if p.starts_at else None,ends_at=p.ends_at.astimezone(timezone.utc).isoformat() if p.ends_at else None)
def catalog_router(database,settings,current_user):
 router=APIRouter()
 async def allowed(token,write=False):
  u=await current_user(token)
  require_business_admin(u,settings)
  return u
 @router.get('/api/web/admin/catalog')
 async def listing(freeman_session:str|None=Cookie(default=None)):
  actor=await allowed(freeman_session)
  async with database.sessions.begin() as s:
   repo=SQLRepository(s);vpn=[];codes=[]
   for t in await repo.tariffs():
    meta=json.loads(await repo.setting(f'catalog_vpn:{t.id}','{}'))
    if meta.get('deleted'):continue
    vpn.append(dict(id=t.id,months=t.months,price=t.price,devices=t.device_limit,name=await repo.tariff_setting(t, 'tariff_name',f'{t.months} мес.'),enabled=await repo.tariff_setting(t, 'tariff_enabled','1')=='1',traffic_gb=meta.get('traffic_gb',0),archived=meta.get('archived',False),deleted=meta.get('deleted',False),retirement_reason=meta.get('retirement_reason',''),version=meta.get('version',0)))
   from app.services.proxy_control import plans
   proxy=[]
   for p in await plans(repo):
    meta=json.loads(await repo.setting(f'catalog_proxy:{p.id}','{}'))
    proxy.append(dict(id=p.id,name=p.name,days=p.days,price=p.price,ip_limit=p.ip_limit,enabled=p.enabled,version=meta.get('version',0),archived=meta.get('archived',False),retirement_reason=meta.get('retirement_reason','')))
   for p in await s.scalars(select(PromoCode).order_by(PromoCode.id.desc()).limit(100)):
    meta=json.loads(await repo.setting(f'promo_rules:{p.code}','{}'))
    codes.append(dict(id=p.id,name=p.code,effect=p.effect,value=p.value,enabled=await repo.setting(f'promo_enabled:{p.code}','1')=='1',uses=await s.scalar(select(func.count()).select_from(PromoActivation).where(PromoActivation.promo_id==p.id)),**meta))
   promos=[json.loads(v) for v in (await repo.settings_prefix('public_promotion:')).values()]
   history=[dict(action=a.action,date=a.created_at.isoformat(),details=json.loads(a.details)) for a in await s.scalars(select(AdminAudit).where(AdminAudit.action.in_(('catalog_change','catalog_delete'))).order_by(AdminAudit.id.desc()).limit(30))]
   return dict(vpn=vpn,proxy=proxy,promotions=promos,codes=codes,history=history,can_edit=settings.is_owner(actor.telegram_id))
 @router.delete('/api/web/admin/catalog/vpn/{tid}')
 async def remove(tid:int,version:int,freeman_session:str|None=Cookie(default=None)):
  actor=await allowed(freeman_session,True)
  async with database.sessions.begin() as s:
   from app.services.catalog_lifecycle import retire
   repo=SQLRepository(s)
   try:result=await retire(repo,'vpn',tid,version)
   except ValueError as error:raise HTTPException(409,str(error)) from error
   if result is None:raise HTTPException(404,'Тариф не найден')
   s.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='catalog_delete',target='vpn:'+str(tid),details=json.dumps(dict(kind='vpn',**result))))
   return result
 @router.post('/api/web/admin/catalog')
 async def save(p:Change,freeman_session:str|None=Cookie(default=None)):
  actor=await allowed(freeman_session,True)
  if not p.confirmed:raise HTTPException(409,'Подтвердите изменения')
  if not p.name.strip():raise HTTPException(422,'Укажите название')
  dt=dates(p);finger=hashlib.sha256(p.model_dump_json().encode()).hexdigest()
  async with database.sessions.begin() as s:
   repo=SQLRepository(s);await s.execute(__import__('sqlalchemy').update(User).where(User.id==actor.id).values(status=User.status))
   marker='catalog_request:'+p.request_id;old=await repo.setting(marker)
   if old:
    x=json.loads(old)
    if x['fingerprint']!=finger:raise HTTPException(409,'Этот запрос уже использован')
    return x['result']
   before={}
   if p.kind=='vpn':
    from app.services.customer_identity import lock_nonce
    if p.id:await lock_nonce(s,'catalog:vpn:'+str(p.id))
    t=await s.get(Tariff,p.id) if p.id else None
    if p.id and not t:
     removed=json.loads(await repo.setting(f'catalog_vpn:{p.id}','{}'))
     raise HTTPException(409 if removed.get('purged') else 404,'Тариф удалён. Обновите список' if removed.get('purged') else 'Тариф не найден')
    if t and t.months!=p.months:raise HTTPException(409,'Срок существующего тарифа сохраняется для истории. Создайте новый тариф')
    meta=json.loads(await repo.setting(f'catalog_vpn:{t.id}','{}')) if t else {}
    if meta.get('deleted'):raise HTTPException(409,'Тариф удалён из каталога; история сохранена')
    if p.version!=meta.get('version',0):raise HTTPException(409,'Тариф изменён. Обновите страницу')
    before=dict(price=t.price,devices=t.device_limit) if t else {}
    if not t:t=await repo.add(Tariff(months=p.months,price=amount(p.price),device_limit=p.devices))
    t.price=amount(p.price);t.device_limit=p.devices
    await repo.set_tariff_setting(t,'tariff_deleted','1' if p.archived else '0')
    await repo.set_tariff_setting(t,'tariff_name',p.name.strip());await repo.set_tariff_setting(t,'tariff_enabled','1' if p.enabled and not p.archived else '0')
    await repo.set_setting(f'catalog_vpn:{t.id}',json.dumps(dict(version=p.version+1,traffic_gb=p.traffic_gb,archived=p.archived)))
    await repo.set_setting(f'tariff_traffic:{t.id}',str(p.traffic_gb*1024**3));rid=t.id
   elif p.kind=='code':
    import re
    code=p.name.strip().upper()
    if not re.fullmatch('[A-Z0-9_-]{3,64}',code):raise HTTPException(422,'Промокод: 3–64 латинских буквы, цифры, дефис или подчёркивание')
    c=await s.get(PromoCode,p.id) if p.id else None
    if p.id and (not c or c.code!=code):raise HTTPException(409,'Код существующего промокода менять нельзя')
    if c and c.effect!='percent_discount':raise HTTPException(409,'Этот промокод управляется в Telegram-админке')
    if not c and await repo.promo(code):raise HTTPException(409,'Промокод уже существует')
    try:value=int(p.value);assert 1<=value<=99
    except (ValueError,AssertionError):raise HTTPException(422,'Скидка должна быть от 1 до 99%')
    meta=json.loads(await repo.setting(f'promo_rules:{code}','{}'))
    if p.version!=meta.get('version',0):raise HTTPException(409,'Промокод изменён. Обновите страницу')
    before=dict(value=c.value) if c else {}
    if not c:c=await repo.add(PromoCode(code=code,effect='percent_discount',value=value))
    c.value=value;await repo.set_setting(f'promo_enabled:{code}','1' if p.enabled else '0');await repo.set_setting(f'promo_rules:{code}',json.dumps(dict(version=p.version+1,ends_at=dt['ends_at'])));rid=c.id
   else:
    try:value=int(p.value) if p.discount_kind=='percent' else amount(p.value);assert p.discount_kind!='percent' or 1<=value<=99
    except (ValueError,AssertionError):raise HTTPException(422,'Процент скидки — от 1 до 99')
    if p.product=='all' and p.plan_ids:raise HTTPException(422,'Для выбранных тарифов укажите продукт')
    if p.plan_ids:
     from app.database.models import ProxyPlan
     model=Tariff if p.product=='vpn' else ProxyPlan
     if await s.scalar(select(func.count()).select_from(model).where(model.id.in_(set(p.plan_ids))))!=len(set(p.plan_ids)):raise HTTPException(422,'Выбранный тариф не найден')
    old=await repo.setting(f'public_promotion:{p.id}') if p.id else None
    if p.id and not old:raise HTTPException(404,'Акция не найдена')
    before=json.loads(old) if old else {}
    if p.version!=before.get('version',0):raise HTTPException(409,'Акция изменена. Обновите страницу')
    rid=p.id or max([int(k.split(':')[1]) for k in (await repo.settings_prefix('public_promotion:'))]+[0])+1
    await repo.set_setting(f'public_promotion:{rid}',json.dumps(dict(id=rid,name=p.name.strip(),product=p.product,target=p.target,kind=p.discount_kind,value=value,plan_ids=p.plan_ids,enabled=p.enabled,version=p.version+1,**dt),ensure_ascii=False))
   result=dict(ok=True,id=rid)
   await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='catalog_change',target=f'{p.kind}:{rid}',details=json.dumps(dict(kind=p.kind,before=before,after=p.model_dump(mode='json'),request_id=p.request_id),ensure_ascii=False)))
   await repo.set_setting(marker,json.dumps(dict(fingerprint=finger,result=result)));return result
 return router
