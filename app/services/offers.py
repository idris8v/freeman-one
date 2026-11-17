"""Shared single/bulk personal-offer domain. Uses the existing campaign outboxes.

Money is integer kopeks. A personal offer competes with the existing price; it
can optionally reduce the standard quote; two personal offers never compound.
Payment reservations enforce the configured usage limit.
"""
import json
import re
from datetime import timedelta, timezone
from decimal import Decimal, InvalidOperation
from sqlalchemy import select, update
from app.database.models import (User,AccessKey,Subscription,VlessCredential,VpnCredential,Tariff,
    PersonalOffer,CustomerContact,OfferDelivery,OfferNotification,OfferEvent,Delivery,EmailDelivery,now)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def offer_status(offer):
    return 'expired' if offer.status=='active' and utc(offer.expires_at)<=now() else offer.status


async def lock_customer(repo,uid):
    await repo.session.execute(update(User).where(User.id==uid).values(status=User.status))
    # A Stars invoice that never passed precheckout can safely expire. Pending
    # provider payments remain reserved until a confirmed cancellation/receipt.
    from app.database.models import Payment
    abandoned=list(await repo.session.scalars(select(Payment).where(Payment.user_id==uid,Payment.status=='stars_created',Payment.created_at<now()-timedelta(minutes=30))))
    for payment in abandoned:
        payment.status='stars_expired'
        await release(repo,payment)


async def has_active(repo,uid):
    from app.services.client_registry import active_key
    return bool(await repo.session.scalar(select(AccessKey.id).where(AccessKey.user_id==uid,active_key()).limit(1)))


async def subscription_id(repo,key):
    for cls in (VlessCredential,VpnCredential):
        row=await repo.session.scalar(select(cls).where(cls.access_key_id==key.id))
        if row:return row.subscription_id
    for sub in await repo.session.scalars(select(Subscription).where(Subscription.user_id==key.user_id)):
        if await repo.setting(f'subscription_key:{sub.id}')==str(key.id):return sub.id
    return None


async def eligible(repo,key):
    return key.status!='deleted' and (utc(key.expires_at)<=now() or key.status!='active') and not await has_active(repo,key.user_id)


def discount_price(offer,base):
    discount=base*offer.value//10000 if offer.discount_type=='percent' else offer.value
    return max(1,base-discount)


async def selection(repo,uid,tariff,sid=0,existing=None):
    rows=await repo.session.scalars(select(PersonalOffer).where(PersonalOffer.user_id==uid,PersonalOffer.status=='active',PersonalOffer.expires_at>now(),PersonalOffer.reserved_payment_id.is_(None)).order_by(PersonalOffer.id.desc()))
    best=None;price=tariff.price if existing is None else existing
    for offer in rows:
        if offer.target in ('message','proxy'):continue
        if offer.target=='renewal' and not sid:continue
        if offer.target=='tariffs' and tariff.id not in json.loads(offer.tariff_ids):continue
        p=discount_price(offer,tariff.price if existing is None or offer.no_stack else existing)
        if p<price:best,price=offer,p
    return best,price


async def reserve(repo,payment,tariff,sid,amount,existing=None):
    await lock_customer(repo,payment.user_id)
    from app.services.pricing import base_quote
    existing,_,special=await base_quote(repo,payment.user_id,tariff)
    if not special:
        from app.services.catalog import public_price
        existing=min(existing,await public_price(repo,'vpn',tariff.id,tariff.price,bool(sid)))
    offer,price=await selection(repo,payment.user_id,tariff,sid,existing)
    if offer and price==amount:
        offer.reserved_payment_id=payment.id
        offer.discount_amount=max(0,existing-price)
        await repo.session.flush()
    return offer


async def consume(repo,payment,total=None):
    offer=await repo.session.scalar(select(PersonalOffer).where(PersonalOffer.reserved_payment_id==payment.id,PersonalOffer.user_id==payment.user_id).with_for_update())
    if offer and offer.status=='active':
        # An already-priced, reserved invoice is honored even if paid after expiry.
        offer.use_count+=1
        offer.status='used' if offer.usage_limit and offer.use_count>=offer.usage_limit else 'active';offer.used_at=now();offer.payment_id=payment.id
        offer.reserved_payment_id=None
        offer.revenue+=payment.amount if total is None else total
        await repo.set_setting(f'sale_offer:{payment.id}',json.dumps(dict(offer_id=offer.id,campaign_id=offer.campaign_id,user_id=offer.user_id,subscription_id=payment.subscription_id,discount_amount=offer.discount_amount,revenue=payment.amount if total is None else total)))


async def release(repo,payment):
    await repo.session.execute(update(PersonalOffer).where(PersonalOffer.reserved_payment_id==payment.id,PersonalOffer.status=='active').values(reserved_payment_id=None,discount_amount=0))


class Channel:
    code='';label=''
    def available(self,settings):return True
    async def enqueue(self,repo,settings,offer,contact):raise NotImplementedError


class TelegramChannel(Channel):
    code='telegram';label='Telegram'
    def available(self,settings):return bool(settings.bot_token)
    async def enqueue(self,repo,settings,offer,contact):
        job=await repo.add(Delivery(dedup=f'offer:{offer.id}:{contact["id"]}',user_id=offer.user_id,campaign_id=offer.campaign_id,text=offer.title+'\n\n'+offer.message+'\n\n'+offer_link(settings,offer),photo='offer:'+str(offer.id)))
        return dict(telegram_job_id=job.id)


class EmailChannel(Channel):
    code='email';label='Email'
    def available(self,settings):
        from app.services.email_delivery import ready
        return ready(settings)
    async def enqueue(self,repo,settings,offer,contact):
        job=await repo.add(EmailDelivery(dedup=f'offer:{offer.id}:{contact["id"]}',user_id=offer.user_id,subscription_id=offer.subscription_id,kind='offer',expiry=utc(offer.expires_at).isoformat()))
        return dict(email_job_id=job.id)


class WhatsAppChannel(Channel):
    code='whatsapp';label='WhatsApp'
    def available(self,settings):return False  # Requires an explicitly configured official provider.


class InternalChannel(Channel):
    code='internal';label='Freeman ONE'
    async def enqueue(self,repo,settings,offer,contact):
        n=await repo.add(OfferNotification(user_id=offer.user_id,offer_id=offer.id,title=offer.title,text=offer.message,target=offer_link(settings,offer)))
        return dict(notification_id=n.id,status='sent')


CHANNELS={c.code:c for c in (TelegramChannel(),EmailChannel(),WhatsAppChannel(),InternalChannel())}


def offer_link(settings,offer):
    if offer.target=='proxy':return settings.web_base_url.rstrip('/')+f'/?view=proxy&proxy_offer={offer.id}'
    if offer.target=='message':return settings.web_base_url.rstrip('/')+'/?view=profile'
    return settings.web_base_url.rstrip('/')+f'/?offer={offer.id}'+(f'&renew_key={offer.key_id}' if offer.target=='renewal' else '')


def masked(code,destination):
    if code=='email':
        a,_,b=destination.partition('@');return a[:1]+'•••@'+b
    if code=='whatsapp':return destination[:2]+'••••••'+destination[-4:]
    return destination


async def contacts(repo,settings,user):
    enabled=user.status!='blocked' and await repo.setting(f'notifications:{user.id}','1')=='1'
    values=[]
    if user.telegram_id:values.append(dict(id='telegram:account',channel='telegram',destination=str(user.telegram_id),label='@'+user.username if user.username else str(user.telegram_id),verified=True))
    if user.email:values.append(dict(id='email:account',channel='email',destination=user.email,label=masked('email',user.email),verified=user.email_verified))
    values.append(dict(id='internal:account',channel='internal',destination=str(user.id),label=user.freeman_id or str(user.id),verified=True))
    extra=await repo.session.scalars(select(CustomerContact).where(CustomerContact.user_id==user.id,CustomerContact.enabled.is_(True)))
    for row in extra:
        if row.destination.startswith('ai:'):continue # Security binding; channel delivery belongs to Freeman AI.
        if any(c['channel']==row.channel and c['destination']==row.destination for c in values):continue
        if row.channel not in CHANNELS:continue
        values.append(dict(id='contact:'+str(row.id),channel=row.channel,destination=row.destination,label=masked(row.channel,row.destination),verified=row.verified))
    for c in values:
        adapter=CHANNELS[c['channel']];c['name']=adapter.label
        valid_destination=c['channel']!='telegram' or bool(re.fullmatch(r'-?[1-9][0-9]*',str(c.get('destination',''))))
        c['available']=bool(enabled and c['verified'] and valid_destination and adapter.available(settings))
        c['reason']='' if c['available'] else 'Уведомления отключены' if not enabled else 'Контакт не подтверждён' if not c['verified'] else 'Нужен числовой Telegram Chat ID' if not valid_destination else 'Провайдер не настроен'
    for code,adapter in CHANNELS.items():
        if not any(c['channel']==code for c in values):values.append(dict(id=code+':missing',channel=code,name=adapter.label,label='',available=False,reason='WhatsApp не подключён' if code=='whatsapp' else 'Контакт не указан'))
    return values


VARIABLES={'name','freeman_id','discount','expiration_date','previous_plan'}
def render_template(text,values):
    for variable in re.findall(r'\{([^{}]*)\}',text):
        if variable not in VARIABLES:raise ValueError('Неизвестная переменная: '+variable)
    if re.search(r'[{}]',re.sub(r'\{(?:name|freeman_id|discount|expiration_date|previous_plan)\}','',text)):raise ValueError('Некорректные переменные сообщения')
    return re.sub(r'\{([^{}]+)\}',lambda m:values[m[1]],text)


async def context(repo,settings,key):
    user=await repo.session.get(User,key.user_id);sid=await subscription_id(repo,key)
    sub=await repo.session.get(Subscription,sid) if sid else None
    tariff=await repo.session.get(Tariff,sub.tariff_id) if sub and sub.tariff_id else None
    history=list(await repo.session.scalars(select(PersonalOffer).where(PersonalOffer.user_id==user.id).order_by(PersonalOffer.id.desc()).limit(20)))
    tariffs=[]
    for t in await repo.session.scalars(select(Tariff).order_by(Tariff.months)):
        if await repo.tariff_setting(t, 'tariff_enabled','1')=='1':tariffs.append(dict(id=t.id,months=t.months,device_limit=t.device_limit,price=t.price,name=f'{t.months} мес. · {t.device_limit} устр.'))
    can_renew=bool(sub and (key.status=='active' or (key.status=='billing_blocked' and await repo.setting(f'billing_blocked:{key.id}')=='1')) and await repo.setting(f'subscription_status:{sub.id}','active')=='active' and await repo.setting(f'subscription_removed:{sub.id}')!='1' and await repo.setting(f'subscription_transferred:{sub.id}')!='1')
    return dict(can_renew=can_renew,user=dict(id=user.id,name=user.display_name or user.username or user.freeman_id,freeman_id=user.freeman_id),key_id=key.id,subscription_id=sid,eligible=bool(sub and await eligible(repo,key)),expires_at=utc(key.expires_at).isoformat(),previous_plan=f'{tariff.months} мес. · {sub.device_limit} устр.' if tariff else f'До {sub.device_limit} устройств' if sub else key.name,contacts=[{k:v for k,v in c.items() if k!='destination'} for c in await contacts(repo,settings,user)],tariffs=tariffs,history=[await serialize(repo,settings,o) for o in history],cooldown=bool(history and utc(history[0].created_at)>now()-timedelta(days=7)))


async def validate(repo,settings,payload,client_mode=False):
    key=await repo.session.get(AccessKey,payload.key_id) if payload.key_id else None
    if client_mode:
        user=await repo.session.get(User,payload.user_id)
        if not user or user.status=='blocked':raise ValueError('Клиент недоступен')
        if not key:key=await repo.session.scalar(select(AccessKey).where(AccessKey.user_id==user.id,AccessKey.status!='deleted').order_by(AccessKey.expires_at.desc()).limit(1))
        if key and key.user_id!=user.id:raise ValueError('Ключ принадлежит другому клиенту')
        if key:ctx=await context(repo,settings,key)
        else:
            from types import SimpleNamespace
            key=SimpleNamespace(id=None,user_id=user.id,name='Нет подписок')
            ctx=dict(can_renew=False,subscription_id=None,user=dict(id=user.id,name=user.display_name,freeman_id=user.freeman_id),previous_plan='Нет подписок',tariffs=[dict(id=t.id,price=t.price) for t in await repo.tariffs() if await repo.tariff_setting(t, 'tariff_enabled','1')=='1'],cooldown=bool(await repo.session.scalar(select(PersonalOffer.id).where(PersonalOffer.user_id==user.id,PersonalOffer.created_at>now()-timedelta(days=7)).limit(1))))
        ctx['eligible']=True
    else:
        if not key:raise ValueError('Ключ не найден')
        ctx=await context(repo,settings,key)
    if payload.target=='renewal' and not ctx['can_renew']:raise ValueError('Этот ключ остановлен вручную. Выберите предложение на новую подписку.')
    if not ctx['eligible']:raise ValueError('У клиента есть активная подписка или ключ недоступен')
    try:value=Decimal(str(payload.value))
    except InvalidOperation:raise ValueError('Некорректная скидка')
    if not value.is_finite() or value<=0 or (payload.discount_type=='percent' and value>=100):raise ValueError('Укажите положительную скидку; процент меньше 100')
    if payload.discount_type not in ('percent','fixed') or payload.target not in ('any','renewal','tariffs','proxy'):raise ValueError('Некорректные параметры предложения')
    if value*100!=int(value*100):raise ValueError('Скидка должна иметь не более двух знаков после запятой')
    value=int(value*100)
    ids=list(set(payload.tariff_ids));actual={t['id']:t for t in ctx['tariffs']}
    if (payload.target!='proxy' and any(i not in actual for i in ids)) or (payload.target=='tariffs' and not ids):raise ValueError('Выберите действующие тарифы')
    if payload.target=='proxy':
        from app.services.proxy_control import plans
        actual={t.id:dict(id=t.id,price=t.price) for t in await plans(repo) if t.enabled}
        if any(i not in actual for i in ids):raise ValueError('Неизвестный тариф Proxy')
    affected=[actual[i] for i in ids] if payload.target in ('tariffs','proxy') and ids else list(actual.values())
    if not affected:raise ValueError('Нет действующих тарифов')
    if payload.discount_type=='fixed' and any(value>=t['price'] for t in affected):raise ValueError('Сумма скидки должна быть меньше цены каждого выбранного тарифа')
    expires=utc(payload.expires_at) if payload.expires_at else now()+timedelta(days=payload.days)
    if expires<=now() or expires>now()+timedelta(days=366):raise ValueError('Срок предложения должен быть в будущем, не более года')
    user=await repo.session.get(User,key.user_id)
    available={c['id']:c for c in await contacts(repo,settings,user) if c['available']}
    selected=list(available.values()) if payload.all_contacts else [available[i] for i in payload.contacts if i in available]
    if not payload.all_contacts and any(i not in available for i in payload.contacts):raise ValueError('Выбранный канал или контакт недоступен')
    if not selected:raise ValueError('Выберите доступный канал отправки')
    discount=f'{Decimal(value)/100:g}'+('%' if payload.discount_type=='percent' else ' ₽')
    values=dict(name=ctx['user']['name'],freeman_id=user.freeman_id or str(user.id),discount=discount,expiration_date=expires.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M МСК'),previous_plan=ctx['previous_plan'])
    title=render_template(payload.title,values);message=render_template(payload.message,values)
    if not title.strip() or not message.strip() or len(title)>255 or len(message)>3000:raise ValueError('Заполните заголовок и сообщение; сообщение до 3000 символов')
    return key,ctx,dict(value=value,expires_at=expires,title=title,message=message,tariff_ids=json.dumps(ids)),selected


async def serialize(repo,settings,offer):
    deliveries=[]
    for d in await repo.session.scalars(select(OfferDelivery).where(OfferDelivery.offer_id==offer.id)):
        status=d.status
        if d.telegram_job_id:
            j=await repo.session.get(Delivery,d.telegram_job_id);status=j.status if j else 'failed'
        if d.email_job_id:
            j=await repo.session.get(EmailDelivery,d.email_job_id);status=j.status if j else 'failed'
        if d.notification_id:
            n=await repo.session.get(OfferNotification,d.notification_id);status='read' if n and n.read_at else 'sent'
        deliveries.append(dict(id=d.id,channel=d.channel,contact=masked(d.channel,d.destination),status={'pending':'queued','sending':'sending'}.get(status,status),error=d.error or ('Ошибка отправки' if status=='failed' else '')))
    clicked=await repo.session.scalar(select(OfferEvent.created_at).where(OfferEvent.offer_id==offer.id,OfferEvent.kind=='clicked').order_by(OfferEvent.id).limit(1))
    return dict(clicked_at=utc(clicked).isoformat() if clicked else None,id=offer.id,campaign_id=offer.campaign_id,usage_limit=offer.usage_limit,use_count=offer.use_count,status=offer_status(offer),discount_type=offer.discount_type,value=offer.value/100,target=offer.target,expires_at=utc(offer.expires_at).isoformat(),created_at=utc(offer.created_at).isoformat(),used_at=utc(offer.used_at).isoformat() if offer.used_at else None,title=offer.title,message=offer.message,link=offer_link(settings,offer),deliveries=deliveries,discount_amount=offer.discount_amount,revenue=offer.revenue,reserved=bool(offer.reserved_payment_id and offer.status=='active'))


async def profile_offers(repo,settings,uid):
    rows=await repo.session.scalars(select(PersonalOffer).where(PersonalOffer.user_id==uid).order_by(PersonalOffer.id.desc()).limit(50))
    offers=[await serialize(repo,settings,o) for o in rows if o.target!='message']
    notices=await repo.session.scalars(select(OfferNotification).where(OfferNotification.user_id==uid).order_by(OfferNotification.id.desc()).limit(50))
    return dict(offers=offers,notifications=[dict(id=n.id,title=n.title,text=n.text,target=n.target,created_at=utc(n.created_at).isoformat(),read=bool(n.read_at),offer_id=n.offer_id) for n in notices])


def active_customers_query():
    from app.services.client_registry import active_key
    return select(AccessKey.user_id).where(active_key()).distinct()
