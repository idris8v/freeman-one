"""Contextual Keys entry point into the shared offer/campaign service."""
from app.web.access import require_business_admin
import hashlib
import json
from datetime import datetime
from fastapi import APIRouter,Cookie,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select,update
from app.database.models import PersonalOffer,Campaign,AccessKey,User,OfferDelivery,OfferNotification,OfferEvent,Delivery,EmailDelivery,AdminAudit,now
from app.database.repositories import SQLRepository
from app.services import offers as domain
from app.web.notification_views import present_notifications, notification_state_router


class OfferPayload(BaseModel):
    key_id:int|None=None
    user_id:int|None=None
    user_ids:list[int]=Field(default_factory=list,max_length=5000)
    filters:dict|None=None
    channels:list[str]=Field(default_factory=lambda:['internal','telegram','email'])
    one_time:bool=True
    no_stack:bool=True
    kind:str='offer'
    discount_type:str='percent'
    value:float=20
    target:str='renewal'
    tariff_ids:list[int]=Field(default_factory=list,max_length=100)
    days:int=Field(default=7,ge=1,le=366)
    expires_at:datetime|None=None
    all_contacts:bool=True
    contacts:list[str]=Field(default_factory=list,max_length=100)
    title:str=Field(default='Мы оставили для вас скидку {discount} 🎁',max_length=255)
    message:str=Field(default='Здравствуйте, {name}!\n\nДля вас действует персональное предложение до {expiration_date}.',max_length=3000)
    request_id:str=Field(default='',max_length=80)
    confirmed:bool=False
    force:bool=False


class OfferAction(BaseModel):
    action:str
    confirmed:bool=False


def offers_router(database,settings,current_user):
    router=APIRouter()
    async def allowed(token,write=False):
        actor=await current_user(token)
        require_business_admin(actor,settings)
        return actor

    @router.get('/api/web/admin/offers/context')
    async def context(key_id:int,freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session)
        async with database.sessions() as session:
            key=await session.get(AccessKey,key_id)
            if not key:raise HTTPException(404,'Ключ не найден')
            return await domain.context(SQLRepository(session),settings,key)

    @router.post('/api/web/admin/offers/preview')
    async def preview(payload:OfferPayload,freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session)
        async with database.sessions() as session:
            try:key,ctx,data,selected=await domain.validate(SQLRepository(session),settings,payload)
            except ValueError as e:raise HTTPException(422,str(e)) from e
            return dict(title=data['title'],message=data['message'],expires_at=data['expires_at'].isoformat(),channels=[dict(channel=c['channel'],name=c['name'],contact=c['label']) for c in selected],customer=ctx['user'])

    @router.post('/api/web/admin/offers')
    async def create(payload:OfferPayload,freeman_session:str|None=Cookie(default=None)):
        actor=await allowed(freeman_session,True)
        if not payload.confirmed or len(payload.request_id)<10:raise HTTPException(422,'Подтвердите отправку предложения')
        fingerprint=hashlib.sha256(json.dumps(payload.model_dump(mode='json',exclude={'request_id','confirmed','force'}),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        async with database.sessions.begin() as session:
            repo=SQLRepository(session)
            key=await session.get(AccessKey,payload.key_id)
            if not key:raise HTTPException(404,'Ключ не найден')
            await domain.lock_customer(repo,key.user_id)
            idem=str(actor.id)+':'+payload.request_id
            old=await session.scalar(select(PersonalOffer).where(PersonalOffer.request_id==idem))
            if old:
                if old.fingerprint!=fingerprint:raise HTTPException(409,'Этот запрос уже использован для другого предложения')
                return await domain.serialize(repo,settings,old)
            try:key,ctx,data,selected=await domain.validate(repo,settings,payload)
            except ValueError as e:raise HTTPException(422,str(e)) from e
            if ctx['cooldown'] and not payload.force:raise HTTPException(409,'Предложение уже отправлялось в последние 7 дней. Подтвердите повторную отправку.')
            campaign=await repo.add(Campaign(actor_id=actor.telegram_id,audience='personal_offer',text=data['message'],status='queued'))
            offer=await repo.add(PersonalOffer(user_id=key.user_id,key_id=key.id,subscription_id=ctx['subscription_id'],campaign_id=campaign.id,created_by=actor.id,request_id=idem,fingerprint=fingerprint,discount_type=payload.discount_type,target=payload.target,**data))
            for contact in selected:
                refs=await domain.CHANNELS[contact['channel']].enqueue(repo,settings,offer,contact)
                await repo.add(OfferDelivery(offer_id=offer.id,contact_id=contact['id'],channel=contact['channel'],destination=contact['destination'],**refs))
            await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='offer.create',target=str(offer.id),details=json.dumps(dict(customer_id=key.user_id,campaign_id=campaign.id,type=offer.discount_type,value=offer.value,channels=[c['channel'] for c in selected]),ensure_ascii=False)))
            return await domain.serialize(repo,settings,offer)

    async def audience(session,payload):
        from app.web.admin_dashboard import AdminQuery
        from app.services.client_registry import query
        if payload.filters is not None:
            filters=AdminQuery(**payload.filters)
            stmt=await query(session,settings,filters)
            ids=list(await session.scalars(stmt.with_only_columns(User.id).limit(5001)))
        else:ids=list(dict.fromkeys(payload.user_ids))
        if not ids:raise ValueError('Выберите клиентов или сегмент')
        if len(ids)>5000:raise ValueError('Для одной кампании выберите не более 5000 клиентов; разделите аудиторию фильтрами')
        return ids

    async def prepare_bulk(session,payload):
        ids=await audience(session,payload);repo=SQLRepository(session);valid=[];skipped=[];counts={c:0 for c in domain.CHANNELS};sample=[]
        if payload.kind not in ('offer','message'):raise ValueError('Неизвестный тип кампании')
        if any(c not in domain.CHANNELS for c in payload.channels):raise ValueError('Неизвестный канал отправки')
        for uid in ids:
            user=await session.get(User,uid)
            if not user or user.status=='blocked':skipped.append(dict(id=uid,reason='Клиент заблокирован или не найден'));continue
            available=[c for c in await domain.contacts(repo,settings,user) if c['available'] and (payload.all_contacts or c['channel'] in payload.channels)]
            if not available:skipped.append(dict(id=uid,reason='Нет доступных выбранных каналов'));continue
            p=payload.model_copy(update=dict(user_id=uid,key_id=None,all_contacts=False,contacts=[c['id'] for c in available]))
            try:
                if payload.kind=='offer':key,ctx,data,selected=await domain.validate(repo,settings,p,client_mode=True)
                else:
                    # Common safe renderers and outboxes; a message never affects pricing.
                    from types import SimpleNamespace
                    from datetime import timedelta
                    key=SimpleNamespace(id=None,user_id=uid);ctx=dict(subscription_id=None,cooldown=False)
                    expires=domain.utc(payload.expires_at) if payload.expires_at else now()+timedelta(days=payload.days)
                    if expires<=now():raise ValueError('Укажите будущий срок')
                    values=dict(name=user.display_name,freeman_id=user.freeman_id or str(uid),discount='',expiration_date=expires.strftime('%d.%m.%Y'),previous_plan='')
                    data=dict(title=domain.render_template(payload.title,values),message=domain.render_template(payload.message,values),value=0,expires_at=expires,tariff_ids='[]');selected=available
            except ValueError as e:skipped.append(dict(id=uid,reason=str(e)));continue
            if ctx.get('cooldown') and not payload.force:skipped.append(dict(id=uid,reason='Уже получал предложение за последние 7 дней'));continue
            valid.append((key,ctx,data,selected))
            for code in {c['channel'] for c in selected}:counts[code]+=1
            if len(sample)<3:sample.append(dict(user_id=uid,name=user.display_name,title=data['title'],message=data['message']))
        if not valid:raise ValueError('В выбранной аудитории нет доступных получателей')
        return ids,valid,dict(total=len(ids),recipients=len(valid),channels=counts,skipped=skipped,sample=sample,user_ids=ids)

    @router.get('/api/web/admin/offers/bulk/context')
    async def bulk_context(freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session)
        async with database.sessions() as session:
            repo=SQLRepository(session)
            return dict(tariffs=[dict(id=t.id,name=f'{t.months} мес. · {t.device_limit} устр.',price=t.price) for t in await repo.tariffs() if await repo.tariff_setting(t, 'tariff_enabled','1')=='1'],channels=[dict(code=c.code,name=c.label,available=c.available(settings)) for c in domain.CHANNELS.values()])

    @router.post('/api/web/admin/offers/bulk/preview')
    async def bulk_preview(payload:OfferPayload,freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session)
        async with database.sessions() as session:
            try:return (await prepare_bulk(session,payload))[2]
            except ValueError as e:raise HTTPException(422,str(e)) from e

    @router.post('/api/web/admin/offers/bulk')
    async def bulk_create(payload:OfferPayload,freeman_session:str|None=Cookie(default=None)):
        actor=await allowed(freeman_session,True)
        if not payload.confirmed or len(payload.request_id)<10:raise HTTPException(422,'Подтвердите запуск кампании')
        fingerprint=hashlib.sha256(json.dumps(payload.model_dump(mode='json',exclude={'request_id','confirmed'}),sort_keys=True).encode()).hexdigest()
        async with database.sessions.begin() as session:
            await session.execute(update(User).where(User.id==actor.id).values(status=User.status))
            repo=SQLRepository(session);stamp=f'client_campaign:{actor.id}:{payload.request_id}';old=await repo.setting(stamp)
            if old:
                previous=json.loads(old)
                if previous['fingerprint']!=fingerprint:raise HTTPException(409,'Запрос уже использован для другой кампании')
                return dict(campaign_id=previous['campaign_id'],queued=previous['queued'])
            try:ids,recipients,summary=await prepare_bulk(session,payload)
            except ValueError as e:raise HTTPException(422,str(e)) from e
            campaign=await repo.add(Campaign(actor_id=actor.telegram_id,audience='client_offer' if payload.kind=='offer' else 'client_message',text=payload.message,status='queued'))
            for key,ctx,data,selected in recipients:
                await domain.lock_customer(repo,key.user_id)
                offer=await repo.add(PersonalOffer(user_id=key.user_id,key_id=key.id,subscription_id=ctx['subscription_id'],campaign_id=campaign.id,created_by=actor.id,request_id=f'{actor.id}:{payload.request_id}:{key.user_id}',fingerprint=fingerprint,discount_type=payload.discount_type,target=payload.target if payload.kind=='offer' else 'message',usage_limit=1 if payload.one_time else None,no_stack=payload.no_stack,**data))
                for contact in selected:
                    refs=await domain.CHANNELS[contact['channel']].enqueue(repo,settings,offer,contact)
                    await repo.add(OfferDelivery(offer_id=offer.id,contact_id=contact['id'],channel=contact['channel'],destination=contact['destination'],**refs))
            await repo.set_setting(stamp,json.dumps(dict(fingerprint=fingerprint,campaign_id=campaign.id,queued=len(recipients))))
            await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='client_campaign.create',target=str(campaign.id),details=json.dumps(dict(recipients=len(recipients),skipped=len(summary['skipped'])))))
            return dict(campaign_id=campaign.id,queued=len(recipients),skipped=summary['skipped'])

    @router.get('/api/web/admin/offers/bulk')
    async def bulk_history(freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session)
        from sqlalchemy import func
        async with database.sessions() as session:
            rows=(await session.execute(select(Campaign,func.count(PersonalOffer.id)).join(PersonalOffer,PersonalOffer.campaign_id==Campaign.id).where(Campaign.audience.in_(('client_offer','client_message'))).group_by(Campaign.id).order_by(Campaign.id.desc()).limit(30))).all()
            return dict(items=[dict(id=c.id,status=c.status,recipients=count,text=c.text[:100],created_at=c.created_at.isoformat()) for c,count in rows])

    @router.get('/api/web/admin/offers/bulk/{campaign_id}')
    async def bulk_status(campaign_id:int,freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session)
        async with database.sessions() as session:
            rows=list(await session.scalars(select(PersonalOffer).where(PersonalOffer.campaign_id==campaign_id)))
            if not rows:raise HTTPException(404,'Кампания не найдена')
            stats={};converted=0;revenue=0
            for o in rows:
                if o.use_count:converted+=1
                revenue+=o.revenue
                for d in (await domain.serialize(SQLRepository(session),settings,o))['deliveries']:stats[d['status']]=stats.get(d['status'],0)+1
            from app.database.models import AppSetting
            discount_cost=0
            for value in await session.scalars(select(AppSetting.value).where(AppSetting.key.like('sale_offer:%'),AppSetting.value.like('%\"campaign_id\": '+str(campaign_id)+',%'))):
                sale=json.loads(value);discount_cost+=int(sale.get('discount_amount',0))
            return dict(campaign_id=campaign_id,recipients=len(rows),deliveries=stats,converted=converted,revenue=revenue,discount_cost=discount_cost)

    @router.post('/api/web/admin/offers/bulk/{campaign_id}/retry')
    async def bulk_retry(campaign_id:int,payload:OfferAction,freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session,True)
        if not payload.confirmed or payload.action!='retry':raise HTTPException(422,'Подтвердите повтор ошибок')
        async with database.sessions.begin() as session:
            rows=list(await session.scalars(select(PersonalOffer).where(PersonalOffer.campaign_id==campaign_id,PersonalOffer.status=='active',PersonalOffer.expires_at>now())))
            count=0
            for o in rows:
                for d in await session.scalars(select(OfferDelivery).where(OfferDelivery.offer_id==o.id)):
                    cls,jid=(Delivery,d.telegram_job_id) if d.telegram_job_id else (EmailDelivery,d.email_job_id)
                    if jid:
                        result=await session.execute(update(cls).where(cls.id==jid,cls.status=='failed').values(status='pending',available_at=now(),attempts=0));count+=result.rowcount
            campaign=await session.get(Campaign,campaign_id)
            if campaign:campaign.status='queued'
            return dict(retried=count)

    @router.get('/api/web/admin/offers/{offer_id}')
    async def details(offer_id:int,freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session)
        async with database.sessions() as session:
            offer=await session.get(PersonalOffer,offer_id)
            if not offer:raise HTTPException(404,'Предложение не найдено')
            return await domain.serialize(SQLRepository(session),settings,offer)

    @router.post('/api/web/admin/offers/{offer_id}/action')
    async def action(offer_id:int,payload:OfferAction,freeman_session:str|None=Cookie(default=None)):
        actor=await allowed(freeman_session,True)
        if not payload.confirmed or payload.action not in ('retry','cancel'):raise HTTPException(422,'Подтвердите действие')
        async with database.sessions.begin() as session:
            offer=await session.scalar(select(PersonalOffer).where(PersonalOffer.id==offer_id).with_for_update())
            if not offer:raise HTTPException(404,'Предложение не найдено')
            if domain.offer_status(offer)!='active' or offer.reserved_payment_id:raise HTTPException(409,'Предложение завершено или уже закреплено за оплатой')
            if payload.action=='cancel':
                offer.status='cancelled'
                await session.execute(update(Delivery).where(Delivery.campaign_id==offer.campaign_id,Delivery.status=='pending').values(status='skipped'))
                for d in await session.scalars(select(OfferDelivery).where(OfferDelivery.offer_id==offer.id)):
                    if d.email_job_id:await session.execute(update(EmailDelivery).where(EmailDelivery.id==d.email_job_id,EmailDelivery.status=='pending').values(status='skipped'))
            else:
                campaign=await session.get(Campaign,offer.campaign_id);campaign.status='queued'
                for d in await session.scalars(select(OfferDelivery).where(OfferDelivery.offer_id==offer.id)):
                    cls,job_id=(Delivery,d.telegram_job_id) if d.telegram_job_id else (EmailDelivery,d.email_job_id)
                    if job_id:await session.execute(update(cls).where(cls.id==job_id,cls.status=='failed').values(status='pending',available_at=now(),attempts=0))
            repo=SQLRepository(session)
            await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='offer.'+payload.action,target=str(offer.id),details=''))
            return await domain.serialize(repo,settings,offer)

    @router.get('/api/web/offers')
    async def mine(freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        async with database.sessions() as session:
            result=await domain.profile_offers(SQLRepository(session),settings,user.id)
            result['notifications']=await present_notifications(session,user.id,result['notifications'])
            return result

    @router.get('/api/web/offers/quotes')
    async def quotes(subscription_id:int=0,freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        async with database.sessions() as session:
            repo=SQLRepository(session)
            if subscription_id:
                from app.services.billing import BillingService
                try:await BillingService(repo,settings).valid(user,subscription_id)
                except ValueError as error:raise HTTPException(409,str(error)) from error
            # Shared prices, one HTTP request and request-local setting reads.
            original=repo.setting;cached={}
            async def setting(key,default=''):
                if key not in cached:cached[key]=await original(key,None)
                return default if cached[key] is None else cached[key]
            repo.setting=setting
            from app.services.pricing import quote as price
            items=[]
            tariffs=await repo.tariffs()
            for tariff in tariffs:
                if await repo.tariff_setting(tariff,'tariff_enabled','1')!='1':continue
                amount,discount,special=await price(repo,user.id,tariff,subscription_id)
                items.append(dict(tariff_id=tariff.id,months=tariff.months,amount=amount,discount=discount,special=special))
            from app.web.purchase_antig import options
            antig=await options(repo,[t for t in tariffs if any(i["tariff_id"]==t.id for i in items)]) if not subscription_id else {"enabled":False,"prices":{}}
            return dict(items=items,subscription_id=subscription_id,antig=antig)

    @router.get('/api/web/offers/quote')
    async def quote(months:int|None=None,tariff_id:int|None=None,subscription_id:int=0,freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        async with database.sessions() as session:
            repo=SQLRepository(session);tariff=await repo.select_tariff(months,tariff_id)
            if not tariff or await repo.tariff_setting(tariff,'tariff_enabled','1')!='1':raise HTTPException(404,'Тариф недоступен')
            if subscription_id:
                from app.services.billing import BillingService
                try:await BillingService(repo,settings).valid(user,subscription_id)
                except ValueError as e:raise HTTPException(409,str(e)) from e
            from app.services.pricing import quote as price
            amount,discount,special=await price(repo,user.id,tariff,subscription_id)
            return dict(tariff_id=tariff.id,months=tariff.months,amount=amount,discount=discount,special=special)

    @router.post('/api/web/offers/{offer_id}/open')
    async def opened(offer_id:int,freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        async with database.sessions.begin() as session:
            repo=SQLRepository(session)
            await domain.lock_customer(repo,user.id)
            offer=await session.get(PersonalOffer,offer_id)
            if not offer or offer.user_id!=user.id:raise HTTPException(404,'Предложение не найдено')
            old=await session.scalar(select(OfferEvent.id).where(OfferEvent.offer_id==offer.id,OfferEvent.user_id==user.id,OfferEvent.kind=='clicked'))
            if not old:await repo.add(OfferEvent(offer_id=offer.id,user_id=user.id,kind='clicked'))
            return {'ok':True}

    @router.get('/api/web/notifications')
    async def notification_list(freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        async with database.sessions() as session:
            rows=await session.scalars(select(OfferNotification).where(OfferNotification.user_id==user.id).order_by(OfferNotification.id.desc()).limit(50))
            items=[dict(id=n.id,title=n.title,text=n.text,target=n.target,created_at=domain.utc(n.created_at).isoformat(),read=bool(n.read_at),offer_id=n.offer_id) for n in rows]
            return dict(items=await present_notifications(session,user.id,items))

    @router.post('/api/web/notifications/{notice_id}/read')
    async def read(notice_id:int,freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        async with database.sessions.begin() as session:
            n=await session.get(OfferNotification,notice_id)
            if not n or n.user_id!=user.id:raise HTTPException(404,'Уведомление не найдено')
            n.read_at=n.read_at or now()
            return {'ok':True}
    router.include_router(notification_state_router(database, settings, current_user))
    return router
