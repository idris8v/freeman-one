"""Website owner issuance and access to previously issued gifts.

No payment/provisioning logic is duplicated here. Quotes are committed before
external calls; retries recover the exact bot operation, including after reload.
"""
import json,secrets
from typing import Literal
from urllib.parse import urlencode
from fastapi import APIRouter,Cookie,HTTPException,Response
from pydantic import BaseModel,Field
from sqlalchemy import select,cast,String,literal
from app.database.models import AppSetting,User,AccessKey,now
from app.database.repositories import SQLRepository
from app.services.customer_gifts import CustomerGifts,QUOTE,ORDER
from app.services.bot_gift_links import BotGiftLinks,gift_key
from app.services.bot_catalog import available
from app.services.screens import duration
from app.services import admin_subscription
from app.web.cabinet.context import WebContext

class QuoteRequest(BaseModel):
    kind:Literal['issue','gift']
    tariff_id:int=Field(gt=0,strict=True)
    request_id:str=Field(min_length=12,max_length=80,pattern=r'^[a-zA-Z0-9-]+$')
    name:str=Field(default='',max_length=100)
class Confirmation(BaseModel):
    quote:str=Field(pattern=r'^[0-9a-f]{20}$')
class GiftPurchase(Confirmation):
    method:Literal['balance','invoice']

def subscriptions_router(context:WebContext):
    router=APIRouter();database,settings=context.database,context.settings
    async def lock(repo,user):
        fresh=await repo.session.scalar(select(User).where(User.id==user.id).with_for_update().execution_options(populate_existing=True))
        if not fresh or fresh.status!='active':raise HTTPException(409,'Аккаунт недоступен')
        return fresh
    async def data(repo,token,user,kind=None,allow_cancelled=False):
        raw=await repo.setting(f'web_subscription_quote:{user.id}:'+token,'{}');d=json.loads(raw)
        if d.get('user')!=user.id:raise HTTPException(404,'Подтверждение недоступно')
        if d.get('cancelled') and not allow_cancelled:raise HTTPException(409,'Расчёт отменён. Выберите тариф заново')
        if kind and d.get('request',{}).get('kind')!=kind:raise HTTPException(409,'Откройте нужный сценарий заново')
        return d
    def public_quote(token,d):
        return dict(token=token,kind=d['request']['kind'],amount=d['terms']['total'],months=d['terms']['months'],devices=d['terms']['devices'],name=d['terms']['tariff_name'],expires_at=d['until'])
    def links(token,months,devices):
        url=settings.web_base_url.rstrip('/')+'/?'+urlencode({'gift':token})
        return dict(link=url,telegram_url=f'https://t.me/{settings.bot_username}?start=fg_{token}',share_text=f'Дарю тебе VPN-подписку Freeman ONE: {duration(months)}, устройств: {devices}. Открой ссылку и получи подарок: {url}\nСрок начнётся после получения.')
    async def free_result(repo,token,d):
        g=json.loads(await repo.setting(gift_key(d['gift_token']),'{}'))
        result=dict(id='free-'+token,free=True,months=d['terms']['months'],devices=d['terms']['devices'],amount=0,status='claimed' if g.get('status')=='claimed' else 'claiming' if g.get('user') else 'expired' if g.get('expires','')<now().isoformat() else g.get('status','unavailable'))
        if result['status']=='open':result.update(links(d['gift_token'],result['months'],result['devices']))
        return result
    async def receipt(repo,pid,user):
        service=CustomerGifts(repo,settings)
        try:p,d=await service.order(pid,user)
        except ValueError as error:raise HTTPException(404,str(error)) from error
        result=dict(id=p.id,amount=p.amount,months=d['months'],devices=d['devices'],status='pending' if p.status in ('gift_created','gift_pending') else 'canceled' if p.status in ('gift_canceled','refunded') else 'unavailable')
        if p.status=='gift_pending' and d.get('url'):result['redirect_url']=d['url']
        if d.get('ready') and p.status in ('balance_paid','order_paid'):
            g=json.loads(await repo.setting(gift_key(d['token']),'{}'))
            result['status']='claimed' if g.get('status')=='claimed' else 'claiming' if g.get('user') else g.get('status','unavailable')
            if result['status']=='open':result.update(links(d['token'],d['months'],d['devices']))
            # Only newly created website orders carry this marker. Preserve all
            # older snapshots and shared bot orders byte-for-byte.
            if d.get('origin')=='website':
                raw=await repo.setting('sale_snapshot:'+str(pid),'{}');snap=json.loads(raw)
                if snap and snap.get('source')!='website':
                    snap['source']='website';await repo.set_setting('sale_snapshot:'+str(pid),json.dumps(snap))
        return result
    @router.get('/api/web/subscriptions/options')
    async def options(response:Response,freeman_session:str|None=Cookie(default=None)):
        user=await context.current_user(freeman_session);owner=settings.is_owner(user.telegram_id)
        response.headers['Cache-Control']='private, no-store'
        async with database.sessions() as session:
            repo=SQLRepository(session);tariffs=[]
            from app.services.billing import purchase_limits
            for t in await available(repo) if owner else []:
                n,traffic=await purchase_limits(repo,t)
                terms=await admin_subscription.sale_terms(repo,t)
                amount=terms['total']
                tariffs.append(dict(id=t.id,name=await repo.tariff_setting(t,'tariff_name',duration(t.months)),months=t.months,devices=n,traffic_bytes=traffic,amount=amount))
            return dict(owner=owner,tariffs=tariffs,balance=await repo.balance(user.id),invoice=False,gifts_enabled=False)
    @router.post('/api/web/subscriptions/quote')
    async def prepare(payload:QuoteRequest,freeman_session:str|None=Cookie(default=None)):
        user=await context.current_user(freeman_session);owner=settings.is_owner(user.telegram_id)
        if payload.kind=='gift':raise HTTPException(410,'Создание подарков на сайте больше недоступно')
        if not owner:raise HTTPException(403,'Выдача доступна владельцу')
        request=payload.model_dump(exclude={'request_id'});request['name']=request['name'].strip()
        async with database.sessions.begin() as session:
            repo=SQLRepository(session);await lock(repo,user);key=f'web_subscription_request:{user.id}:{payload.request_id}'
            token=await repo.setting(key)
            if token:
                d=await data(repo,token,user)
                if d['request']!=request:raise HTTPException(409,'Запрос уже связан с другим тарифом')
                return public_quote(token,d)
            t=next((t for t in await available(repo) if t.id==payload.tariff_id),None)
            if not t:raise HTTPException(404,'Тариф недоступен')
            t=await repo.lock_tariff(t)
            try:
                terms=await admin_subscription.sale_terms(repo,t)
            except ValueError as error:raise HTTPException(422,str(error)) from error
            token=secrets.token_hex(10);until=now().timestamp()+600
            d=dict(user=user.id,request=request,terms=terms,until=until)
            await repo.set_setting(key,token);await repo.set_setting(f'web_subscription_quote:{user.id}:'+token,json.dumps(d))
            await repo.set_setting('admin_grant:'+token,json.dumps(dict(actor=user.telegram_id,months=t.months,sid=0,expires=until,sale_terms=terms)))
            return public_quote(token,d)
    @router.post('/api/web/subscriptions/cancel')
    async def cancel(payload:Confirmation,freeman_session:str|None=Cookie(default=None)):
        user=await context.current_user(freeman_session)
        async with database.sessions.begin() as session:
            repo=SQLRepository(session);await lock(repo,user)
            d=await data(repo,payload.quote,user,allow_cancelled=True)
            if d.get('cancelled'):return dict(canceled=True)
            gift=json.loads(await repo.setting(QUOTE+payload.quote,'{}'))
            grant=json.loads(await repo.setting('admin_grant:'+payload.quote,'{}'))
            from app.services.vless_creation import unresolved
            if d.get('done') or d.get('gift_token') or gift.get('payment') or grant.get('result') or await unresolved(repo,user,['admin-grant:'+payload.quote]):
                raise HTTPException(409,'Заказ уже запущен. Продолжите исходный запрос или проверьте подарок в истории')
            d['cancelled']=True
            await repo.set_setting(f'web_subscription_quote:{user.id}:'+payload.quote,json.dumps(d))
            return dict(canceled=True)
    @router.post('/api/web/subscriptions/issue')
    async def issue(payload:Confirmation,freeman_session:str|None=Cookie(default=None)):
        user=await context.owner(freeman_session)
        try:
            async with database.sessions.begin() as session:
                repo=SQLRepository(session);await lock(repo,user);d=await data(repo,payload.quote,user,'issue')
                sid=await admin_subscription.grant(repo,settings,user,d['terms']['months'],0,payload.quote)
                kid=await repo.setting(f'subscription_key:{sid}');key=await session.get(AccessKey,int(kid)) if kid else None
                if not key:raise ValueError('Выдача ещё проверяется. Повторите исходное подтверждение.')
                # Name both shared fields once, never overwrite a later rename.
                if not d.get('done'):
                    name=d['request']['name'] or await repo.setting(f'subscription_name:{sid}',f'F R E E M A N · #{sid}')
                    key.name=name;await repo.set_setting(f'subscription_name:{sid}',name)
                    d['done']=dict(subscription_id=sid,key_id=key.id,name=name,status='issued')
                    await repo.set_setting(f'web_subscription_quote:{user.id}:'+payload.quote,json.dumps(d))
                return d['done']
        except ValueError as error:raise HTTPException(409,str(error)) from error
    @router.post('/api/web/gifts/free',deprecated=True)
    async def free(payload:Confirmation,freeman_session:str|None=Cookie(default=None)):
        await context.owner(freeman_session)
        raise HTTPException(410,'Создание подарков на сайте больше недоступно')
    @router.post('/api/web/gifts/purchase',deprecated=True)
    async def purchase(payload:GiftPurchase,freeman_session:str|None=Cookie(default=None)):
        await context.current_user(freeman_session)
        raise HTTPException(410,'Покупка подарков на сайте больше недоступна')
    @router.get('/api/web/gifts')
    async def gifts(response:Response,freeman_session:str|None=Cookie(default=None)):
        user=await context.current_user(freeman_session);response.headers['Cache-Control']='private, no-store'
        async with database.sessions.begin() as session:
            repo=SQLRepository(session)
            from app.database.models import Payment
            ids=list(await session.scalars(select(Payment.id).join(AppSetting,AppSetting.key==literal(ORDER)+cast(Payment.id,String)).where(Payment.user_id==user.id).order_by(Payment.id.desc()).limit(50)))
            items=[await receipt(repo,pid,user)for pid in ids]
            if settings.is_owner(user.telegram_id):
                rows=await session.execute(select(AppSetting.key,AppSetting.value).where(AppSetting.key.startswith(f'web_subscription_quote:{user.id}:',autoescape=True)).order_by(AppSetting.key).limit(1000))
                for key,value in rows:
                    d=json.loads(value)
                    if d.get('user')==user.id and d.get('gift_token'):items.append(await free_result(repo,key.rsplit(':',1)[1],d))
            return dict(items=items)
    @router.get('/api/web/gifts/{ref}')
    async def gift(ref:str,response:Response,freeman_session:str|None=Cookie(default=None)):
        user=await context.current_user(freeman_session);response.headers['Cache-Control']='private, no-store'
        async with database.sessions.begin() as session:
            repo=SQLRepository(session)
            if ref.startswith('free-'):
                d=await data(repo,ref[5:],user,'gift')
                if not d.get('gift_token'):raise HTTPException(404,'Подарок не найден')
                return await free_result(repo,ref[5:],d)
            if not ref.isdecimal() or len(ref)>12:raise HTTPException(404,'Подарок не найден')
            return await receipt(repo,int(ref),user)
    @router.post('/api/web/gifts/{pid}/check')
    async def check(pid:int,freeman_session:str|None=Cookie(default=None)):
        user=await context.current_user(freeman_session)
        async with database.sessions.begin() as session:
            repo=SQLRepository(session);service=CustomerGifts(repo,settings)
            try:await service.render(f'cg:check:{pid}',user)
            except ValueError as error:raise HTTPException(409,str(error)) from error
            return await receipt(repo,pid,user)
    async def invitation(repo,token):
        try:key=gift_key(token)
        except ValueError as error:raise HTTPException(404,'Подарок не найден') from error
        d=json.loads(await repo.setting(key,'{}'))
        if not d or d.get('kind')!='vpn':raise HTTPException(404,'Подарок не найден')
        if d.get('status') not in ('open','claiming','claimed') or not d.get('paid') and d.get('expires','')<now().isoformat():raise HTTPException(410,'Приглашение больше не действует')
        return d
    @router.get('/api/web/gift-invitation/{token}')
    async def preview(token:str,response:Response):
        response.headers.update({'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
        async with database.sessions() as session:
            d=await invitation(SQLRepository(session),token)
            return dict(months=d.get('months'),period=d.get('label','Подписка'),devices=d.get('devices'),status=d['status'],telegram_url=f'https://t.me/{settings.bot_username}?start=fg_{token}')
    @router.post('/api/web/gift-invitation/{token}/claim')
    async def claim(token:str,freeman_session:str|None=Cookie(default=None)):
        user=await context.current_user(freeman_session)
        try:
            # reserve() has its own durable transaction. Don't hold a user lock
            # outside it; the same invitation can be resumed through Telegram.
            async with database.sessions.begin() as session:
                repo=SQLRepository(session);await invitation(repo,token)
                await BotGiftLinks(repo,settings).claim(token,user)
                d=json.loads(await repo.setting(gift_key(token),'{}'))
                if d.get('user')!=user.id or d.get('status')!='claimed':raise ValueError('Подключение ещё проверяется. Повторите получение по этой же ссылке.')
                kid=await repo.setting(f'subscription_key:{d["sid"]}')
                return dict(status='claimed',subscription_id=d['sid'],key_id=int(kid),name=await repo.setting(f'subscription_name:{d["sid"]}','Подарок'))
        except (ValueError,PermissionError) as error:raise HTTPException(409,str(error)) from error
    return router
