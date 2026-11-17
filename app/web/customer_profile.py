from app.web.request_performance import measured_route
from app.services.accounts import display_freeman_id
"""Administrator workspace backed by the shared customer, service and ledger domains."""
from app.web.access import require_business_admin
import json,hashlib
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from decimal import Decimal,InvalidOperation
from cryptography.fernet import Fernet,InvalidToken
from fastapi import APIRouter,Cookie,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select,func,or_,text
from app.database.models import *
from app.database.repositories import SQLRepository
from app.services.offers import utc,contacts,lock_customer
from app.services.client_registry import query as customer_query,active_key
from app.web.admin_dashboard import AdminQuery
from app.web.payout_details import masked_details
TZ=ZoneInfo('Europe/Moscow')
class CustomerAction(BaseModel):
 action:str=Field(pattern='^(credit|debit|block|unblock|note|reset_devices)$')
 amount:str=''
 reason:str=Field(default='',max_length=180)
 comment:str=Field(default='',max_length=2000)
 confirmed:bool=False
 request_id:str=Field(min_length=10,max_length=64,pattern=r'^[a-zA-Z0-9-]+$')
def iso(v):return utc(v).isoformat() if v else None
async def referral_summary(repo,user):
 s=repo.session;month=now().astimezone(TZ).replace(day=1,hour=0,minute=0,second=0,microsecond=0)
 invited=select(Referral.invitee_id).where(Referral.inviter_id==user.id).union(select(User.id).where(User.referrer_id==user.id)).subquery()
 paid=('succeeded','order_paid','stars_paid','balance_paid','manual_paid','proxy_order_paid')
 earned,monthly=(await s.execute(select(func.coalesce(func.sum(ReferralReward.amount),0),func.coalesce(func.sum(__import__('sqlalchemy').case((ReferralReward.created_at>=month,ReferralReward.amount),else_=0)),0)).where(ReferralReward.inviter_id==user.id,ReferralReward.status=='credited'))).one()
 return dict(invited=await s.scalar(select(func.count()).select_from(invited)),buyers=await s.scalar(select(func.count(func.distinct(Payment.user_id))).where(Payment.user_id.in_(select(invited.c.invitee_id)),Payment.status.in_(paid),or_(Payment.subscription_id.is_not(None),Payment.status.in_(('proxy_order_paid','order_paid','stars_paid'))))),earned=int(earned),month=int(monthly),link=f'{settings_base(repo)}/r/{display_freeman_id(user.freeman_id)}' if user.freeman_id else None)
def settings_base(repo):return getattr(repo,'profile_base','')
async def summary(repo,settings,u,actor):
 repo.profile_base=settings.web_base_url.rstrip('/')
 row=(await repo.session.execute((await customer_query(repo.session,settings,AdminQuery(),user_ids=[u.id])).where(User.id==u.id))).one()
 _,ac,total,pc,seen,nearest,expired,status=row
 available=[c for c in await contacts(repo,settings,u) if c.get('destination')]
 for c in available:
  if c['channel']=='email':c['label']=c['destination']
  if c['channel']=='telegram' and c['id']=='telegram:account':c['label']='@'+u.username if u.username else 'Telegram '+str(u.telegram_id)
 raw=await repo.setting(f'external_contact:{u.id}')
 if raw:
  try:
   phone=Fernet(settings.encryption_key.encode()).decrypt(raw.encode()).decode();available.append(dict(id='phone:account',channel='phone',name='Телефон',destination=phone,label=phone,available=False,reason='Контакт для поиска и связи'))
  except (ValueError,InvalidToken):pass
 payout=None;payout_error=None
 raw=await repo.setting(f'web:payout-details:{u.id}')
 if raw:
  try:payout=masked_details(json.loads(Fernet(settings.encryption_key.encode()).decrypt(raw.encode()).decode()))
  except (ValueError,InvalidToken,KeyError):payout_error='Не удалось загрузить реквизиты'
 try:note=json.loads(await repo.setting(f'customer_note:{u.id}','null'))
 except ValueError:note=None
 jobs=[json.loads(v) for v in await repo.session.scalars(select(AppSetting.value).where(AppSetting.key.startswith(f'customer_job:{u.id}:')))]
 return dict(user=dict(id=u.id,name=u.display_name,username=u.username,freeman_id=u.freeman_id,telegram_id=u.telegram_id,email=u.email,avatar=f'/api/web/admin/users/{u.id}/avatar' if u.telegram_id else None,status=status,created_at=iso(u.created_at),last_activity=datetime.fromtimestamp(seen,timezone.utc).isoformat() if seen else None),balance=await repo.balance(u.id),contacts=available,payout=payout,payout_error=payout_error,referral=await referral_summary(repo,u),counts=dict(active_keys=ac,total_keys=total,active_proxy=pc,proxy=await repo.session.scalar(select(func.count()).select_from(ProxyCredential).where(ProxyCredential.user_id==u.id,ProxyCredential.status.not_in(('deleted','migrated'))))),note=note,controls=dict(change_balance=settings.is_owner(actor.telegram_id),issue_key=settings.is_owner(actor.telegram_id),issue_proxy=True,block=settings.is_owner(actor.telegram_id),note=True,reset_devices=settings.is_owner(actor.telegram_id)),jobs=dict(pending=sum(v['status']=='pending' for v in jobs),failed=sum(v['status']=='error' for v in jobs)))
async def section(repo,settings,u,name,page,limit):
 s=repo.session;offset=(page-1)*limit
 if name=='keys':
  count=await s.scalar(select(func.count()).select_from(AccessKey).where(AccessKey.user_id==u.id,AccessKey.status!='deleted'))
  records=(await s.execute(select(AccessKey,active_key().label('valid')).where(AccessKey.user_id==u.id,AccessKey.status!='deleted').order_by(AccessKey.id.desc()).offset(offset).limit(limit))).all();ids=[k.id for k,valid in records]
  vless={c.access_key_id:c for c in await s.scalars(select(VlessCredential).where(VlessCredential.access_key_id.in_(ids)))};vpn={c.access_key_id:c for c in await s.scalars(select(VpnCredential).where(VpnCredential.access_key_id.in_(ids)))}
  links={int(x.value):int(x.key.split(':')[1]) for x in await s.scalars(select(AppSetting).where(AppSetting.key.startswith('subscription_key:'),AppSetting.value.in_([str(v) for v in ids]))) if x.value.isdigit()}
  subids={c.subscription_id for c in [*vless.values(),*vpn.values()]}|set(links.values());subs={v.id:v for v in await s.scalars(select(Subscription).where(Subscription.id.in_(subids)))}
  tariffs={v.id:v for v in await s.scalars(select(Tariff).where(Tariff.id.in_([v.tariff_id for v in subs.values()])))}
  devices=dict((await s.execute(select(Device.subscription_id,func.count()).where(Device.subscription_id.in_(subids),Device.status.not_in(('removed','deleted','disconnected'))).group_by(Device.subscription_id))).all())
  items=[]
  for k,valid in records:
   c=vless.get(k.id) or vpn.get(k.id);sub=subs.get(c.subscription_id if c else links.get(k.id));tariff=tariffs.get(sub.tariff_id) if sub else None
   device_count=(devices.get(sub.id,0) if sub else None) if k.mode=='mock' else None
   if sub and k.id in vless:
    from app.services.vless_hwid import cache_state
    device_count,_=await cache_state(s,sub.id)
   items.append(dict(id=k.id,name=k.name,status='active' if valid else 'expired' if utc(k.expires_at)<=now() else 'blocked' if k.status in ('blocked','billing_blocked','disabled') else k.status,created_at=iso(k.created_at),expires_at=iso(k.expires_at),subscription_id=sub.id if sub else None,months=tariff.months if tariff else None,device_limit=sub.device_limit if sub else None,device_count=device_count,traffic=vless[k.id].traffic_used if k.id in vless else (vpn[k.id].received+vpn[k.id].sent) if k.id in vpn else None,provider_available=bool(c),mode=k.mode,can_copy=bool(vpn.get(k.id) or (vless.get(k.id) and vless[k.id].subscription_url))))
 elif name=='proxies':
  from app.services.proxy_control import fresh
  base=select(ProxyCredential,ProxyServer,ProxyAccessDetails).join(ProxyServer,ProxyServer.id==ProxyCredential.server_id).outerjoin(ProxyAccessDetails,ProxyAccessDetails.credential_id==ProxyCredential.id).where(ProxyCredential.user_id==u.id,ProxyCredential.status.not_in(('deleted','migrated')))
  count=await s.scalar(select(func.count()).select_from(base.subquery()));items=[]
  keylinks=dict((await s.execute(select(VlessCredential.subscription_id,VlessCredential.access_key_id).where(VlessCredential.user_id==u.id))).all())
  for c,server,m in await s.execute(base.order_by(ProxyCredential.id.desc()).offset(offset).limit(limit)):
   items.append(dict(id=c.id,server=server.name,source=m.source if m else 'unknown',subscription_id=m.subscription_id if m else None,key_id=keylinks.get(m.subscription_id) if m else None,payment_id=m.payment_id if m else None,status='expired' if utc(c.expires_at)<=now() else 'blocked' if c.status=='disabled' else c.status,expires_at=iso(c.expires_at),created_at=iso(c.created_at),ips=c.active_unique_ips if fresh(server) else None,ip_limit=m.ip_limit if m else None,traffic=c.total_octets if c.last_sync else None))
 elif name=='payments':
  from app.web.admin_payments import LEDGER,parameters,present
  p=parameters(datetime(2000,1,1,tzinfo=TZ),now()+timedelta(days=1))|dict(uid=u.id,limit=limit,offset=offset)
  count=(await s.execute(text(LEDGER+'SELECT count(*) FROM ledger WHERE user_id=:uid'),p)).scalar_one()
  items=[present(r) for r in (await s.execute(text(LEDGER+'SELECT * FROM ledger WHERE user_id=:uid ORDER BY dt DESC,id DESC LIMIT :limit OFFSET :offset'),p)).mappings()]
 elif name=='support':
  count=await s.scalar(select(func.count()).select_from(Ticket).where(Ticket.user_id==u.id));msg=select(TicketMessage.text).where(TicketMessage.ticket_id==Ticket.id,TicketMessage.role=='client').order_by(TicketMessage.id).limit(1).scalar_subquery()
  items=[dict(id=t.id,status=t.status,created_at=iso(t.created_at),updated_at=iso(t.updated_at),title=(m or 'Обращение с вложением')[:160]) for t,m in await s.execute(select(Ticket,msg).where(Ticket.user_id==u.id).order_by(Ticket.updated_at.desc()).offset(offset).limit(limit))]
 elif name=='referrals':
  base=select(ReferralReward,User).outerjoin(User,User.id==ReferralReward.referred_user_id).where(ReferralReward.inviter_id==u.id);count=await s.scalar(select(func.count()).select_from(base.subquery()))
  items=[dict(id=r.id,user_id=v.id if v else None,name=v.display_name if v else 'Историческое начисление',amount=r.amount,status=r.status,transaction_id=r.transaction_id,payment_id=r.payment_id,created_at=iso(r.created_at),purchase_type=r.purchase_type) for r,v in await s.execute(base.order_by(ReferralReward.created_at.desc()).offset(offset).limit(limit))]
 elif name=='activity':
  from sqlalchemy import literal,union_all,case
  events=union_all(select(AccessKey.created_at.label('at'),literal('Выдан VPN-ключ').label('label'),AccessKey.name.label('detail')).where(AccessKey.user_id==u.id),select(ProxyCredential.created_at,literal('Создан Telegram Proxy'),func.concat('Proxy #',ProxyCredential.id)).where(ProxyCredential.user_id==u.id),select(BalanceTransaction.created_at,literal('Операция баланса'),BalanceTransaction.description).where(BalanceTransaction.user_id==u.id),select(Ticket.created_at,literal('Создано обращение'),func.concat('#',Ticket.id)).where(Ticket.user_id==u.id),select(FunnelEvent.created_at,case({'start':'Открыл сервис','plans':'Просмотр тарифов','trial':'Запрос пробной подписки','checkout':'Начато оформление','paid':'Оплачена подписка'},value=FunnelEvent.stage),literal('')).where(FunnelEvent.user_id==u.id,FunnelEvent.stage.in_(('start','plans','trial','checkout','paid'))),select(User.created_at,literal('Регистрация'),User.registration_source).where(User.id==u.id),select(AdminAudit.created_at,literal('Выдача / продление VPN'),AdminAudit.details).where(AdminAudit.action=='manual_issuance',AdminAudit.target==str(u.id))).subquery()
  count=await s.scalar(select(func.count()).select_from(events));items=[dict(date=iso(at),title=label,detail=detail) for at,label,detail in await s.execute(select(events).order_by(events.c.at.desc()).offset(offset).limit(limit))]
 else:raise HTTPException(404,'Раздел не найден')
 return dict(items=items,total=count,page=page,limit=limit)
def customer_profile_router(database,settings,current_user):
 router=APIRouter(route_class=measured_route(database.engine))
 async def admin(token,owner=False):
  actor=await current_user(token)
  require_business_admin(actor,settings)
  return actor
 async def user(repo,uid):
  u=await repo.session.get(User,uid)
  if not u:raise HTTPException(404,'Клиент не найден')
  return u
 @router.get('/api/web/admin/customers/{uid}/profile')
 async def profile(uid:int,freeman_session:str|None=Cookie(default=None)):
  actor=await admin(freeman_session)
  async with database.sessions() as s:
   repo=SQLRepository(s);return await summary(repo,settings,await user(repo,uid),actor)
 @router.get('/api/web/admin/customers/{uid}/section/{name}')
 async def sections(uid:int,name:str,page:int=1,limit:int=10,freeman_session:str|None=Cookie(default=None)):
  await admin(freeman_session)
  if page<1 or limit not in (3,5,10,20,50):raise HTTPException(422,'Некорректная страница')
  async with database.sessions() as s:
   repo=SQLRepository(s);return await section(repo,settings,await user(repo,uid),name,page,limit)
 @router.post('/api/web/admin/customers/{uid}/chat/open')
 async def open_chat(uid:int,freeman_session:str|None=Cookie(default=None)):
  await admin(freeman_session)
  from app.services.customer_identity import lock_nonce
  async with database.sessions.begin() as s:
   repo=SQLRepository(s);u=await user(repo,uid)
   if u.status=='deleted':raise HTTPException(404,'Клиент недоступен')
   await lock_nonce(s,'web:customer-chat:'+str(uid))
   conv=await s.scalar(select(AIConversation).where(AIConversation.user_id==uid,AIConversation.channel!='owner',AIConversation.mode.not_in(('deleted','closed'))).order_by(AIConversation.updated_at.desc()).limit(1))
   if conv:cid=str(conv.id)
   else:
    ticket=await s.scalar(select(Ticket).where(Ticket.user_id==uid,Ticket.status!='closed').order_by(Ticket.updated_at.desc()).limit(1))
    if ticket:cid='ticket-'+str(ticket.id)
    else:
     conv=AIConversation(user_id=uid,channel='website',mode='human');s.add(conv);await s.flush();cid=str(conv.id)
   return dict(url='/admin/ai?chat='+cid)
 @router.post('/api/web/admin/customers/{uid}/action')
 async def action(uid:int,p:CustomerAction,freeman_session:str|None=Cookie(default=None)):
  actor=await admin(freeman_session,p.action!='note')
  try:
   if p.action!='note' and not p.confirmed:raise HTTPException(409,'Подтвердите действие')
   async with database.sessions.begin() as s:
    repo=SQLRepository(s);await lock_customer(repo,uid);u=await user(repo,uid)
    fingerprint=hashlib.sha256(p.model_dump_json().encode()).hexdigest();marker=f'customer_action:{uid}:{p.request_id}';old=await repo.setting(marker)
    if old:
     record=json.loads(old)
     if record['fingerprint']!=fingerprint:raise HTTPException(409,'Идентификатор уже использован для другой операции')
     return record['result']
    before=await repo.balance(uid) if p.action in ('credit','debit') else u.status;result=dict(ok=True)
    if p.action in ('credit','debit'):
     try:
      amount=Decimal(p.amount.replace(',','.'))*100
      if amount!=amount.to_integral_value() or not 1<=amount<=100000000:raise ValueError()
      amount=int(amount)
     except (InvalidOperation,ValueError):raise HTTPException(422,'Укажите сумму от 0,01 до 1 000 000 ₽ с точностью до копейки')
     if not p.reason.strip():raise HTTPException(422,'Укажите причину')
     if p.action=='debit' and before<amount:raise HTTPException(409,'Недостаточно средств на балансе')
     tx=await repo.add(BalanceTransaction(user_id=uid,type='admin_credit' if p.action=='credit' else 'admin_debit',amount=amount if p.action=='credit' else -amount,description=(p.reason.strip()+(' · '+p.comment.strip() if p.comment.strip() else ''))[:255]));result.update(transaction_id=tx.id,balance=await repo.balance(uid))
    elif p.action=='note':await repo.set_setting(f'customer_note:{uid}',json.dumps(dict(text=p.comment.strip(),updated_by=actor.id,updated_at=iso(now())) if p.comment.strip() else None,ensure_ascii=False))
    else:
     from app.services.customer_controls import enqueue
     if p.action in ('block','unblock') and settings.is_admin(u.telegram_id):raise HTTPException(409,'Нельзя блокировать административный аккаунт')
     if not p.reason.strip():raise HTTPException(422,'Укажите причину')
     result=await enqueue(repo,settings,u,actor,p.action,p.reason)
    after=result.get('balance',u.status)
    await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id or -actor.id,action='customer_'+p.action,target=str(uid),details=json.dumps(dict(before=before,after=after,reason=p.reason,comment=p.comment,request_id=p.request_id,result=result),ensure_ascii=False)[:4096]))
    await repo.set_setting(marker,json.dumps(dict(fingerprint=fingerprint,result=result)));return result
  except HTTPException as error:
   async with database.sessions.begin() as rejected:
    rejected.add(AdminAudit(actor_telegram_id=actor.telegram_id or -actor.id,action='customer_'+p.action,target=str(uid),details=json.dumps(dict(result='rejected',reason=p.reason,request_id=p.request_id,error=error.detail),ensure_ascii=False)[:4096]))
   raise

 return router
