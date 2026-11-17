"""Provider-hosted card saving and explicit recurring consent. No PAN/CVV storage."""
import hashlib,json,uuid
from datetime import datetime,timezone,timedelta
from sqlalchemy import select,update
from app.database.models import User,Payment,Subscription,AppSetting,ChatNotice,now
from app.database.repositories import SQLRepository
from app.services.topups import YooKassa,SubscriptionCheckoutService
from app.services.pricing import quote
from app.services.billing import BillingService
async def methods(repo,uid,include_expired=False):
 result=[]
 for row in await repo.session.scalars(select(AppSetting).where(AppSetting.key.startswith(f'card:{uid}:'))):
  m=json.loads(row.value)
  try:valid_expiry=(int(m.get('year',0)),int(m.get('month',0)))>=(now().year,now().month)
  except (ValueError,TypeError):valid_expiry=False
  if m.get('active') and (valid_expiry or include_expired):result.append(m)
 return result
async def unsettled_card(repo,sid):
 for row in await repo.session.scalars(select(AppSetting).where(AppSetting.key.startswith(f'card_cycle:{sid}:'))):
  if not row.value:continue
  p=await repo.session.get(Payment,int(row.value))
  if p and p.status in ('card_created','card_sending','card_uncertain','order_pending','order_processing'):return p
 return None
async def renewal_status(repo,settings,user,sub):
 enabled=await repo.setting(f'auto_renew:{sub.id}','0')=='1'
 master=await repo.setting(f'auto_master:{user.id}','1')=='1'
 source=await repo.setting(f'auto_source:{sub.id}','balance');reason=''
 if enabled:
  if not master:reason='Приостановлено общим режимом'
  else:
   try:await BillingService(repo,settings).valid(user,sub.id)
   except ValueError:reason='Подписка недоступна для продления'
   if not reason and source=='card':
    method=await repo.setting(f'auto_card:{sub.id}','')
    if not any(m['id']==method for m in await methods(repo,user.id)):reason='Карта недоступна. Выберите другую карту'
    elif not settings.yookassa_shop_id or not settings.yookassa_secret_key or await repo.setting('payment_provider:yookassa','1')!='1':reason='Оплата картой временно недоступна'
   pending=await unsettled_card(repo,sub.id)
   if pending and pending.status=='card_uncertain':reason='Результат платежа требует проверки в поддержке'
 return dict(enabled=enabled,master=master,effective=enabled and not reason,source=source,reason=reason)
async def remember(repo,payment,data):
 if await repo.setting(f'card_bind:{payment.id}')!='1':return
 m=data.get('payment_method',{});card=m.get('card',{})
 if m.get('type')!='bank_card' or not m.get('saved') or not m.get('id'):return
 await repo.set_setting(f'card:{payment.user_id}:{m["id"]}',json.dumps(dict(id=m['id'],last4=card.get('last4',''),brand=card.get('card_type','Карта'),month=card.get('expiry_month',''),year=card.get('expiry_year',''),active=True)))
async def start_bind(database,settings,user,nonce):
 if not settings.yookassa_shop_id or not settings.yookassa_secret_key:raise ValueError('Привязка карт сейчас недоступна.')
 key=f'card_bind_order:{user.id}:'+hashlib.sha256(nonce.encode()).hexdigest()
 async with database.sessions.begin() as s:
  await s.execute(update(User).where(User.id==user.id).values(status=User.status));repo=SQLRepository(s)
  if await repo.setting('payment_provider:yookassa','1')!='1':raise ValueError('Оплата через ЮKassa сейчас недоступна.')
  existing=await repo.setting(key)
  payment=await s.get(Payment,int(existing)) if existing else await repo.add(Payment(user_id=user.id,amount=100,status='topup_created'))
  if not existing:await repo.set_setting(key,str(payment.id));await repo.set_setting(f'topup_idem:{payment.id}',str(uuid.uuid4()));await repo.set_setting(f'card_bind:{payment.id}','1')
  pid=payment.id;idem=await repo.setting(f'topup_idem:{pid}');provider_id=payment.provider_id
 if not provider_id and payment.created_at.replace(tzinfo=timezone.utc)<now()-timedelta(hours=1):raise ValueError('Результат привязки не подтверждён. Обратитесь в поддержку перед повторной оплатой.')
 if provider_id:
  async with database.sessions() as s:return dict(url=await SQLRepository(s).setting(f'topup_url:{pid}'),payment_id=pid)
 data=await YooKassa(settings).create(payment,idem,description='Привязка карты · пополнение баланса на 1 ₽',return_url=settings.web_base_url+'/account',save_method=True)
 async with database.sessions.begin() as s:
  p=await s.scalar(select(Payment).where(Payment.id==pid).with_for_update());p.provider_id=data['id'];
  if p.status=='topup_created':p.status='topup_pending'
  url=data.get('confirmation',{}).get('confirmation_url','')
  if not url.startswith('https://'):raise ValueError('ЮKassa не вернула ссылку оплаты.')
  await SQLRepository(s).set_setting(f'topup_url:{pid}',url)
 return dict(url=url,payment_id=pid)
async def prepare_cards(database,settings):
 if not settings.yookassa_shop_id or not settings.yookassa_secret_key:return
 async with database.sessions.begin() as s:
  repo=SQLRepository(s)
  if await repo.setting('payment_provider:yookassa','1')!='1':return
  for sub in await s.scalars(select(Subscription).where(Subscription.expires_at<=now())):
   if await repo.setting(f'auto_renew:{sub.id}','0')!='1' or await repo.setting(f'auto_source:{sub.id}','balance')!='card' or await repo.setting(f'auto_master:{sub.user_id}','1')!='1':continue
   await s.execute(update(User).where(User.id==sub.user_id).values(status=User.status))
   await s.refresh(sub)
   user=await s.get(User,sub.user_id)
   try:await BillingService(repo,settings).valid(user,sub.id)
   except ValueError:continue
   mid=await repo.setting(f'auto_card:{sub.id}','');method=next((m for m in await methods(repo,user.id) if m['id']==mid),None)
   if not method:continue
   cycle=f'card_cycle:{sub.id}:'+hashlib.sha256(sub.expires_at.replace(tzinfo=timezone.utc).isoformat().encode()).hexdigest()
   if await repo.setting(cycle):continue
   if await unsettled_card(repo,sub.id):continue
   months=int(await repo.setting(f'auto_renew_months:{sub.id}','1'));tariff=await repo.auto_tariff(sub.id)
   if not tariff or tariff.device_limit!=sub.device_limit or await repo.tariff_setting(tariff,'tariff_enabled','1')!='1':continue
   from app.services.billing import purchase_limits
   try:devices,traffic_limit=await purchase_limits(repo,tariff)
   except ValueError:continue
   amount=(await quote(repo,user.id,tariff,sub.id))[0]
   if amount<100:continue
   p=await repo.add(Payment(user_id=user.id,amount=amount,status='card_created'))
   from app.services.offers import reserve
   await reserve(repo,p,tariff,sub.id,amount)
   await repo.set_setting(cycle,str(p.id));await repo.set_setting(f'card_attempt:{p.id}',json.dumps(dict(method=mid,sid=sub.id,expiry=sub.expires_at.replace(tzinfo=timezone.utc).isoformat(),attempts=0)))
   await repo.set_setting(f'order:{p.id}',json.dumps(dict(months=tariff.months,tariff_id=tariff.id,sid=sub.id,amount=amount,total=amount,balance_applied=0,devices=devices,traffic_bytes=traffic_limit)))
   await repo.set_setting(f'order_idem:{p.id}',str(uuid.uuid4()))
 async with database.sessions() as s:ids=list(await s.scalars(select(Payment.id).where(Payment.status.in_(['card_created','card_sending'])).limit(30)))
 for pid in ids:
  async with database.sessions.begin() as s:
   repo=SQLRepository(s);p=await s.scalar(select(Payment).where(Payment.id==pid).with_for_update());
   if not p or p.status not in ('card_created','card_sending'):continue
   attempt=json.loads(await repo.setting(f'card_attempt:{pid}'));sub=await s.get(Subscription,attempt['sid']);user=await s.get(User,p.user_id)
   if attempt.get('next_at','')>now().isoformat():continue
   if attempt.get('started_at') and (now()-datetime.fromisoformat(attempt['started_at'])).total_seconds()>3600:
    p.status='card_uncertain';await failure_notice(repo,p,attempt);continue
   if attempt.get('started_at') and (not sub or user.status!='active' or sub.expires_at.replace(tzinfo=timezone.utc).isoformat()!=attempt['expiry'] or await repo.setting('payment_provider:yookassa','1')!='1' or await repo.setting(f'subscription_removed:{attempt["sid"]}')=='1' or await repo.setting(f'subscription_transferred:{attempt["sid"]}')=='1' or await repo.setting(f'auto_master:{p.user_id}','1')!='1' or await repo.setting(f'auto_renew:{attempt["sid"]}','0')!='1' or await repo.setting(f'auto_source:{attempt["sid"]}','balance')!='card' or await repo.setting(f'auto_card:{attempt["sid"]}','')!=attempt['method']):
    p.status='card_uncertain';await failure_notice(repo,p,attempt);continue
   if not attempt.get('started_at') and (not sub or sub.expires_at.replace(tzinfo=timezone.utc).isoformat()!=attempt['expiry'] or user.status!='active' or await repo.setting(f'auto_master:{user.id}','1')!='1' or await repo.setting(f'auto_renew:{sub.id}','0')!='1' or await repo.setting(f'auto_source:{sub.id}','balance')!='card' or await repo.setting(f'auto_card:{sub.id}','')!=attempt['method'] or await repo.setting('payment_provider:yookassa','1')!='1'):
    p.status='order_canceled';await SubscriptionCheckoutService(repo,settings).release_hold(p,await SubscriptionCheckoutService(repo,settings).details(p));continue
   try:
    if not attempt.get('started_at'):await BillingService(repo,settings).valid(user,sub.id)
   except ValueError:
    p.status='order_canceled';await SubscriptionCheckoutService(repo,settings).release_hold(p,await SubscriptionCheckoutService(repo,settings).details(p));continue
   m=next((m for m in await methods(repo,user.id) if m['id']==attempt['method']),None)
   if not m:
    if attempt.get('started_at'):p.status='card_uncertain';await failure_notice(repo,p,attempt)
    else:p.status='order_canceled';await SubscriptionCheckoutService(repo,settings).release_hold(p,await SubscriptionCheckoutService(repo,settings).details(p))
    continue
   order=json.loads(await repo.setting(f'order:{pid}'));idem=await repo.setting(f'order_idem:{pid}')
   attempt.setdefault('started_at',now().isoformat());attempt['next_at']=(now()+timedelta(minutes=2)).isoformat();p.status='card_sending'
   await repo.set_setting(f'card_attempt:{pid}',json.dumps(attempt))
  try:
   data=await YooKassa(settings).request('POST','',dict(amount=dict(value=f'{p.amount/100:.2f}',currency='RUB'),capture=True,payment_method_id=attempt['method'],description=f'Автопродление подписки #{attempt["sid"]}',metadata=dict(freeman_payment=str(pid),freeman_user=str(p.user_id),freeman_kind='subscription',freeman_months=str(order['months']),freeman_sid=str(attempt['sid']))),idem)
   async with database.sessions.begin() as s:
    repo=SQLRepository(s);p=await s.scalar(select(Payment).where(Payment.id==pid).with_for_update())
    if p.status in ('order_paid','order_canceled'):continue
    p.provider_id=data['id'];p.status='order_pending'
    await SubscriptionCheckoutService(repo,settings).credit(p,data)
    if p.status=='order_canceled':await failure_notice(repo,p,attempt)
  except (ValueError,KeyError):
   async with database.sessions.begin() as s:
    repo=SQLRepository(s);p=await s.scalar(select(Payment).where(Payment.id==pid).with_for_update())
    if p.status not in ('card_sending','card_created'):continue
    attempt=json.loads(await repo.setting(f'card_attempt:{pid}'));attempt['attempts']+=1;attempt['next_at']=(now()+timedelta(minutes=5)).isoformat();await repo.set_setting(f'card_attempt:{pid}',json.dumps(attempt))
    p.status='card_uncertain' if attempt['attempts']>=3 else 'card_created'
    if p.status=='card_uncertain':await failure_notice(repo,p,attempt)

async def failure_notice(repo,payment,attempt):
 key=f'card_failure:{payment.id}'
 if await repo.setting(key):return
 await repo.set_setting(key,'1')
 user=await repo.session.get(User,payment.user_id)
 text=(f'Автопродление подписки #{attempt["sid"]}: платёж отменён. Проверьте карту или продлите вручную.' if payment.status=='order_canceled' else f'Автопродление подписки #{attempt["sid"]}: результат платежа не подтверждён. Обратитесь в поддержку перед повторной оплатой.')
 await repo.set_setting(f'web_payment_notice:{payment.user_id}:{payment.id}',json.dumps(dict(text=text,subscription_id=attempt['sid'],payment_id=payment.id,status=payment.status)))
 if user and user.telegram_id:await repo.add(ChatNotice(dedup=key,target_id=user.telegram_id,kind='client',text=text,buttons=json.dumps([[['Платежи и автопродление','payment_management']]])))

