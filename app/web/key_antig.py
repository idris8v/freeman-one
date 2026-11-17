"""Website addon intents over the bot's pricing and durable VLESS operation."""
import json
import secrets
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from app.database.models import User, AccessKey, Subscription, VlessCredential, VlessRenewal, BalanceTransaction, Payment, AdminAudit, now
from app.database.repositories import SQLRepository
from app.services.bot_antig_addon import access, rate_remaining, has_access, deadline
from app.services.bot_antig_price import enabled, tariff_total
from app.services.vless_creation import utc
from app.services.vless_renewal import operation_id

async def addon_active(repo,sub):
    """None means unverified configuration: don't offer a duplicate purchase."""
    if not sub:return None
    try:return await has_access(repo,sub)
    except (ValueError,TypeError,KeyError):return None

PREFIX='web_key_antig:'
POINTER='web_antig_open:'

async def key_context(s,settings,actor,key_id):
    actor=await s.get(User,actor.id,populate_existing=True)
    if not actor or actor.status!='active':raise PermissionError('Аккаунт недоступен')
    key=await s.get(AccessKey,key_id,populate_existing=True)
    admin=settings.is_owner(actor.telegram_id)
    if not key or (key.user_id!=actor.id and not admin):raise PermissionError('Подписка недоступна')
    c=await s.scalar(select(VlessCredential).where(VlessCredential.access_key_id==key_id).execution_options(populate_existing=True))
    sub=await s.get(Subscription,c.subscription_id,populate_existing=True) if c else None
    customer=await s.get(User,key.user_id,populate_existing=True)
    if not sub or not customer or customer.status!='active' or key.mode!='vless' or key.status!='active' or c.status!='active' or sub.user_id!=key.user_id or c.user_id!=key.user_id or utc(sub.expires_at)<=now():raise ValueError('Выберите действующую VPN-подписку VLESS')
    repo=SQLRepository(s)
    if await repo.setting(f'subscription_status:{sub.id}','active')!='active' or await repo.setting(f'subscription_removed:{sub.id}')=='1' or await repo.setting(f'subscription_transferred:{sub.id}')=='1':raise ValueError('Подписка недоступна')
    return repo,actor,key,sub,c,admin

async def locked(s,settings,actor,key_id):
    # Follow the customer -> subscription order used by billing and panel control.
    key=await s.get(AccessKey,key_id)
    if not key:raise PermissionError('Подписка недоступна')
    customer_id=key.user_id
    for uid in sorted({actor.id,customer_id}):await s.scalar(select(User).where(User.id==uid).with_for_update())
    fresh=await s.get(AccessKey,key_id,populate_existing=True)
    if fresh.user_id!=customer_id:raise ValueError("Владелец подписки изменился. Обновите страницу")
    result=await key_context(s,settings,actor,key_id)
    await s.scalar(select(Subscription).where(Subscription.id==result[3].id).with_for_update())
    return await key_context(s,settings,actor,key_id)

async def data_for(repo,actor,key_id,token):
    d=json.loads(await repo.setting(PREFIX+token,'{}'))
    if d.get('actor')!=actor.id or d.get('key')!=key_id:raise PermissionError('Расчёт недоступен')
    return d

async def present(repo,d,token):
    balance=await repo.balance(d['customer'])
    status='done' if d.get('done') else 'failed' if d.get('failed') else 'pending' if d.get('held') else 'expired' if d['until']<now().timestamp() else 'funds' if not d['admin'] and balance<d['amount'] else 'ready'
    return dict(token=token,status=status,amount=d['amount'],balance=balance,expires_at=d['expiry'],remaining_days=d['remaining_days'],full_surcharge=d['full_surcharge'],months=d['months'],admin=d['admin'],key_id=d['key'],name=d['name'],devices=d['devices'])

async def check_other(repo,sub,own_token=None,*,own_operation=None):
    from app.services.recurring_cards import unsettled_card
    if await unsettled_card(repo,sub.id):raise ValueError('Сначала завершите текущий автоплатёж')
    from app.web.subscription_transfer import PENDING
    for p in await repo.session.scalars(select(Payment).where(Payment.user_id==sub.user_id,Payment.status.in_(PENDING))):
        order=json.loads(await repo.setting(f'order:{p.id}','{}'))
        if p.subscription_id==sub.id or order.get('sid')==sub.id:raise ValueError('Сначала завершите текущую оплату этой подписки')
    for pointer,prefix in (('bot_upgrade_open:','bot_upgrade:'),('bot_antig_open:','bot_antig_addon:'),(POINTER,PREFIX)):
        token=await repo.setting(f'{pointer}{sub.user_id}:{sub.id}')
        if token and token!=own_token:
            other=json.loads(await repo.setting(prefix+token,'{}'))
            if other.get('held') and not other.get('done'):raise ValueError('Сначала завершите уже начатое улучшение этой подписки')
    pending=select(VlessRenewal.id).where(VlessRenewal.subscription_id==sub.id,VlessRenewal.state.not_in(('applied','failed')))
    if own_operation is not None:
        pending=pending.where(VlessRenewal.id!=own_operation)
    if await repo.session.scalar(pending.limit(1)):raise ValueError('Панель ещё проверяет предыдущую операцию')

async def create_quote(database,settings,actor,key_id,token=''):
    async with database.sessions.begin() as s:
        repo,actor,key,sub,c,admin=await locked(s,settings,actor,key_id)
        if token:return await present(repo,await data_for(repo,actor,key_id,token),token)
        pending=await repo.setting(f'{POINTER}{sub.user_id}:{sub.id}')
        if pending:
            d=json.loads(await repo.setting(PREFIX+pending,'{}'))
            if d.get('held') and not d.get('done'):
                if d.get('actor')!=actor.id:raise ValueError('Подключение уже выполняется. Дождитесь завершения')
                return await present(repo,d,pending)
        if not await enabled(repo):raise ValueError('Антиглушилка сейчас выключена')
        if await has_access(repo,sub) and await deadline(repo,sub)>=utc(sub.expires_at):return dict(status='done',key_id=key.id,amount=0,admin=admin)
        await check_other(repo,sub)
        stamp=now();amount=0 if admin else await rate_remaining(repo,sub,stamp)
        tariff=await repo.tariff_by_id(sub.tariff_id) if sub.tariff_id else None
        profile,groups=await access(repo,settings,c)
        if type(amount) is not int or not 0<=amount<=100000000:raise ValueError('Некорректная доплата. Обратитесь в поддержку')
        token=secrets.token_hex(16)
        d=dict(actor=actor.id,customer=sub.user_id,admin=admin,key=key.id,sid=sub.id,cid=c.id,remote=c.remote_user_uuid,expiry=utc(sub.expires_at).isoformat(),devices=sub.device_limit,amount=amount,profile=profile,groups=groups,until=stamp.timestamp()+600,remaining_days=(utc(sub.expires_at)-stamp).total_seconds()/86400,full_surcharge=await tariff_total(repo,tariff) if tariff else None,months=tariff.months if tariff else None,name=key.name,held=False,done=False)
        await repo.set_setting(PREFIX+token,json.dumps(d))
        return await present(repo,d,token)

async def apply_quote(database,settings,actor,key_id,token):
    # Commit the hold BEFORE contacting the panel. A dropped HTTP response must
    # not roll it back after the durable provider operation has succeeded.
    async with database.sessions.begin() as s:
        repo,actor,key,sub,c,admin=await locked(s,settings,actor,key_id)
        d=await data_for(repo,actor,key_id,token)
        if d.get('done') or d.get('failed'):return await present(repo,d,token)
        if not d.get('held'):
            if d['until']<now().timestamp():raise ValueError('Расчёт устарел. Обновите сумму')
            if d['customer']!=sub.user_id or d['expiry']!=utc(sub.expires_at).isoformat() or d['devices']!=sub.device_limit or d['cid']!=c.id or d['remote']!=c.remote_user_uuid or d['profile']['panel']!=c.server_id:raise ValueError('Подписка изменилась. Обновите расчёт')
            if not await enabled(repo):raise ValueError('Подключение сейчас выключено')
            tariff=await repo.tariff_by_id(sub.tariff_id) if sub.tariff_id else None
            if not admin and tariff and await tariff_total(repo,tariff)!=d['full_surcharge']:raise ValueError('Цена дополнения изменилась. Проверьте новый расчёт')
            if await has_access(repo,sub) and await deadline(repo,sub)>=utc(sub.expires_at):raise ValueError('Антиглушилка уже подключена. Обновите список')
            await check_other(repo,sub,token)
            if not admin and await repo.balance(sub.user_id)<d['amount']:return await present(repo,d,token)
            if not admin and d['amount']:
                hold=await repo.add(BalanceTransaction(user_id=sub.user_id,type='upgrade_hold',amount=-d['amount'],description=f'Резерв антиглушилки #{sub.id}'));d['hold']=hold.id
            d['held']=True
            await repo.set_setting(PREFIX+token,json.dumps(d));await repo.set_setting(f'{POINTER}{sub.user_id}:{sub.id}',token)
    async with database.sessions.begin() as s:
        repo,actor,key,sub,c,admin=await locked(s,settings,actor,key_id)
        d=await data_for(repo,actor,key_id,token)
        if d.get('done') or d.get('failed'):return await present(repo,d,token)
        if d['customer']!=sub.user_id:raise ValueError('Владелец подписки изменился. Обратитесь в поддержку')
        from app.services.vless_provision import control
        ref='bot-antig:'+token
        try:
            await control(repo,settings,c,'extend',datetime.fromisoformat(d['expiry']),renewal=dict(operation_key=ref,request=dict(kind='bot_antig_upgrade',active_squads=dict(base=d['groups'],target=sorted(set(d['groups'])|set(d['profile']['squads']))),amount=d['amount']),devices=d['devices'],traffic_limit=None))
        except ValueError:
            op=await s.get(VlessRenewal,operation_id(sub.user_id,ref),populate_existing=True)
            if op and op.state!='failed':return await present(repo,d,token)
            if d.get('hold'):s.add(BalanceTransaction(user_id=sub.user_id,type='upgrade_release',amount=d['amount'],description=f'Возврат резерва антиглушилки #{sub.id}'))
            d.update(failed=True,held=False);await repo.set_setting(PREFIX+token,json.dumps(d));return await present(repo,d,token)
        if d.get('hold'):
            hold=await s.get(BalanceTransaction,d['hold']);hold.type='subscription_upgrade';hold.description=f'Антиглушилка #{sub.id}'
        if not admin and d['amount']:
            payment=await repo.add(Payment(user_id=sub.user_id,subscription_id=sub.id,amount=d['amount'],status='balance_paid',provider_id='bot-antig:'+token))
            from app.web.admin_reports import record_sale
            await record_sale(repo,payment,0,d['devices'],renewal=True,source='website')
            snap=json.loads(await repo.setting(f'sale_snapshot:{payment.id}'));snap.update(antig_addon=True,tariff_name='Антиглушилка',tariff_id=sub.tariff_id);await repo.set_setting(f'sale_snapshot:{payment.id}',json.dumps(snap))
        await repo.set_setting(f'bot_test_access:{sub.id}',json.dumps(d['profile']));await repo.set_setting(f'bot_antig_until:{sub.id}',d['expiry']);await repo.set_setting(f'auto_renew:{sub.id}','0')
        d.update(done=True,held=False);await repo.set_setting(PREFIX+token,json.dumps(d))
        if admin:s.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='web_antig_free',target=str(key.id),details=json.dumps(dict(customer=sub.user_id,token=token,expiry=d['expiry']))))
        return await present(repo,d,token)

async def fund_quote(database,settings,actor,key_id,token):
    from app.services.topups import TopupService
    # Stage the provider idempotency key in a committed local invoice first.
    async with database.sessions.begin() as s:
        repo,actor,key,sub,c,admin=await locked(s,settings,actor,key_id)
        d=await data_for(repo,actor,key_id,token)
        if admin or d.get('done') or d.get('held') or d.get('failed'):raise ValueError('Этот расчёт не требует пополнения')
        if not d.get('topup'):
            if d['until']<now().timestamp() or not await enabled(repo):raise ValueError('Расчёт устарел или подключение выключено. Обновите расчёт')
            tariff=await repo.tariff_by_id(sub.tariff_id) if sub.tariff_id else None
            if tariff and await tariff_total(repo,tariff)!=d['full_surcharge']:raise ValueError('Цена дополнения изменилась. Проверьте новый расчёт')
            shortfall=d['amount']-await repo.balance(sub.user_id)
            if shortfall<=0:raise ValueError('Средств уже достаточно. Проверьте баланс')
            screen=await TopupService(repo,settings).prepare(actor,max(100,shortfall))
            action=next((action for row in screen.rows for _,action in row if action.startswith('topup_pay:')),None)
            if not action:raise ValueError('Пополнение сейчас недоступно')
            d['topup']=int(action.split(':')[1]);await repo.set_setting(PREFIX+token,json.dumps(d))
            await repo.set_setting(f"topup_return_url:{d['topup']}",settings.web_base_url.rstrip('/')+'/?antig_key='+str(key.id))
    async with database.sessions.begin() as s:
        repo,actor,key,sub,c,admin=await locked(s,settings,actor,key_id);d=await data_for(repo,actor,key_id,token)
        screen=await TopupService(repo,settings).render('topup_pay:'+str(d['topup']),actor)
        url=next((action[4:] for row in screen.rows for _,action in row if action.startswith('url:https://')),None)
        if not url:raise ValueError('Счёт завершён. Проверьте баланс или обновите расчёт')
        payment=await s.get(Payment,d['topup'])
        return dict(url=url,payment_id=payment.id,amount=payment.amount)

async def check_funding(database,settings,actor,key_id,token):
    from app.services.topups import TopupService
    async with database.sessions.begin() as s:
        repo,actor,key,sub,c,admin=await locked(s,settings,actor,key_id);d=await data_for(repo,actor,key_id,token)
        if d.get('topup') and not admin:await TopupService(repo,settings).render('topup_check:'+str(d['topup']),actor)
        return await present(repo,d,token)

async def disable_addon(database,settings,actor,key_id):
    """Remove just the optional squad through the existing verified VLESS journal."""
    if not settings.is_owner(actor.telegram_id):
        raise PermissionError('Нет прав на управление антиглушилкой')
    from app.services.bot_antig_test import profile
    from app.services.vless_provision import control
    async with database.sessions.begin() as s:
        repo,actor,key,sub,credential,admin=await locked(s,settings,actor,key_id)
        if not admin:
            raise PermissionError('Нет прав на управление антиглушилкой')
        current=await profile(repo,sub.id)
        if not current or len(current['squads'])!=2:
            return {'enabled':False}
        if current['panel']!=credential.server_id:
            raise ValueError('Панель подписки изменилась. Обновите карточку')
        base_squad,optional_squad=current['squads']
        epoch_key=f'web_antig_disable_epoch:{sub.id}'
        epoch=int(await repo.setting(epoch_key,'0'))
        operation_key=f'bot-antig:web-disable:{sub.id}:{epoch}'
        identity=operation_id(sub.user_id,operation_key)
        await check_other(repo,sub,own_operation=identity)
        operation=await s.get(VlessRenewal,identity)
        if operation and operation.state!='failed':
            intent=json.loads(operation.request)['intent']
            if optional_squad not in intent['active_squads']['base'] or optional_squad in intent['active_squads']['target']:
                raise ValueError('Условия операции изменились. Обновите карточку')
        else:
            desired,base=await access(repo,settings,credential)
            if desired!=current or optional_squad not in base:
                raise ValueError('Панель не подтвердила текущий набор серверов')
            intent=dict(kind='bot_antig_expire',active_squads=dict(base=base,target=[item for item in base if item!=optional_squad]))
        await control(repo,settings,credential,'extend',sub.expires_at,renewal=dict(
            operation_key=operation_key,request=intent,devices=credential.device_limit))
        await repo.set_setting(f'bot_test_access:{sub.id}',json.dumps(dict(panel=current['panel'],squads=[base_squad])))
        await repo.set_setting(f'bot_antig_until:{sub.id}','9999-12-31T00:00:00+00:00')
        await repo.set_setting(epoch_key,str(epoch+1))
        s.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='web_antig_disable',target=str(key.id),
                         details=json.dumps(dict(customer=sub.user_id,subscription=sub.id))))
        return {'enabled':False}


class DisableConfirmation(BaseModel):
    confirmed:bool=False


class Confirmation(BaseModel):
    token:str=Field(pattern=r'^[a-f0-9]{32}$')
    confirmed:bool=False

def key_antig_router(database,settings,current_user):
    router=APIRouter()
    async def actor(request):
        user=await current_user(request.cookies.get('freeman_session'))
        if request.method!='GET' and (request.headers.get('sec-fetch-site')=='cross-site' or request.headers.get('origin') not in (None,settings.web_base_url.rstrip('/'))):raise HTTPException(403,'Неверный источник запроса')
        return user
    async def guarded(call):
        try:return await call
        except PermissionError as e:raise HTTPException(403,str(e)) from None
        except ValueError as e:raise HTTPException(409,str(e)) from None
    @router.post('/api/web/admin/keys/{key_id}/antig/disable')
    async def disable(key_id:int,payload:DisableConfirmation,request:Request):
        if not payload.confirmed:
            raise HTTPException(422,'Подтвердите отключение')
        return await guarded(disable_addon(database,settings,await actor(request),key_id))
    @router.get('/api/web/antig/availability')
    async def availability(request:Request):
        await actor(request)
        async with database.sessions() as session:
            return {'enabled':await enabled(SQLRepository(session))}
    @router.post('/api/web/keys/{key_id}/antig/quote')
    async def proposed(key_id:int,request:Request):return await guarded(create_quote(database,settings,await actor(request),key_id))
    @router.get('/api/web/keys/{key_id}/antig/status')
    async def status(key_id:int,token:str,request:Request):
        if len(token)!=32:raise HTTPException(422,'Неверный расчёт')
        return await guarded(create_quote(database,settings,await actor(request),key_id,token))
    @router.post('/api/web/keys/{key_id}/antig/apply')
    async def applied(key_id:int,payload:Confirmation,request:Request):
        if not payload.confirmed:raise HTTPException(422,'Подтвердите подключение')
        return await guarded(apply_quote(database,settings,await actor(request),key_id,payload.token))
    @router.post('/api/web/keys/{key_id}/antig/fund')
    async def funding(key_id:int,payload:Confirmation,request:Request):
        if not payload.confirmed:raise HTTPException(422,'Подтвердите пополнение')
        return await guarded(fund_quote(database,settings,await actor(request),key_id,payload.token))
    @router.post('/api/web/keys/{key_id}/antig/check')
    async def checked(key_id:int,payload:Confirmation,request:Request):
        return await guarded(check_funding(database,settings,await actor(request),key_id,payload.token))
    return router
