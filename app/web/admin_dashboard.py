from app.web.admin_key_rows import key_rows
from app.web.request_performance import measured_route
from app.web.device_names import device_display_name
from app.web.access import require_business_admin
from datetime import timedelta,timezone
from fastapi import APIRouter,Cookie,HTTPException
from fastapi.responses import Response
from pydantic import BaseModel,Field
import json,hashlib
from datetime import datetime
from typing import Literal
from sqlalchemy import select,func,or_,and_,cast,String
from app.database.models import User,Payment,AccessKey,Subscription,VpnServer,VpnCredential,VlessCredential,ProxyCredential,ProxyServer,AdminAudit,Ticket,Device,Tariff,RemnawaveServer,BalanceTransaction,now
from app.database.repositories import SQLRepository

class VpnGrant(BaseModel):
    user_id:int|None=None
    identity:str=Field(default='',max_length=100)
    external_phone:str=Field(default='',max_length=40)
    server_id:int|None=None
    device_limit:int=Field(default=1,ge=0,le=100,strict=True)
    name:str=Field(default='',max_length=100)
    days:int=Field(default=30,ge=1,le=3650,strict=True)
    tariff_id:int|None=Field(default=None,ge=1,strict=True)
    issuance_kind:Literal['manual_sale','complimentary','test']='manual_sale'
    payment_received:bool=Field(default=False,strict=True)
    expected_amount:int|None=Field(default=None,ge=0,le=100000000,strict=True)
    request_id:str=Field(min_length=10,max_length=36,pattern=r'^[a-zA-Z0-9-]+$')

class KeyAction(BaseModel):
    action:str
    value:str=Field(default='',max_length=100)
    confirmed:bool=False
    unit:str='days'
    payment_confirmed:bool=False
    payment_mode:str='free'
    issuance_kind:Literal['manual_sale','complimentary','test']|None=None
    tariff_id:int|None=Field(default=None,ge=1,strict=True)
    expected_amount:int|None=Field(default=None,ge=0,le=100000000,strict=True)
    amount:int=Field(default=0,ge=0,le=100000000)
    expires_at:datetime|None=None
    request_id:str=Field(min_length=10,max_length=36,pattern=r'^[a-zA-Z0-9-]+$')

class ManualQuote(BaseModel):
    tariff_id:int=Field(ge=1,strict=True)
    user_id:int|None=Field(default=None,ge=1,strict=True)
    identity:str=Field(default='',max_length=100)
    external_phone:str=Field(default='',max_length=40)
    key_id:int|None=Field(default=None,ge=1,strict=True)

class AdminQuery(BaseModel):
    q:str=Field(default='',max_length=4096)
    page:int=1
    limit:int=10
    status:str='all'
    origin:str='all'
    devices:str='all'
    expiry:int=0
    tariff_id:int|None=None
    server_id:int|None=None
    customer_state:str='all'
    expired_range:str='all'
    device_limit:int|None=None
    key_state:str='all'
    activity:str='all'
    proxy:str='all'
    contact:str='all'
    sort:str='newest'
    export:bool=False
    selected_ids:list[int]=Field(default_factory=list,max_length=5000)

PAID=('succeeded','order_paid','balance_paid','stars_paid','manual_paid')
def admin_dashboard_router(database,settings,current_user):
    router=APIRouter(route_class=measured_route(database.engine))
    from app.web.remnawave_admin import remnawave_admin_router
    router.include_router(remnawave_admin_router(database,settings,current_user))
    async def allowed(token):
        actor=await current_user(token)
        require_business_admin(actor,settings)
        return actor
    def user_json(u):
        return dict(id=u.id,name=u.display_name,username=u.username,freeman_id=u.freeman_id,telegram_id=u.telegram_id,email=u.email,registration_source=u.registration_source,status=u.status,created_at=u.created_at.isoformat())
    @router.get('/api/web/admin/dashboard')
    async def dashboard(days:int=7,freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session)
        if days not in (1,7,30,90,0):raise HTTPException(422,'Недоступный период')
        async with database.sessions() as session:
            filters=[Payment.status.in_(PAID)]
            if days:filters.append(Payment.created_at>=now()-timedelta(days=days))
            total,count= (await session.execute(select(func.coalesce(func.sum(Payment.amount),0),func.count()).where(*filters))).one()
            active=await session.scalar(select(func.count()).select_from(AccessKey).where(AccessKey.status=='active',AccessKey.expires_at>now()))
            users=await session.scalar(select(func.count()).select_from(User))
            daily=(await session.execute(select(func.date(Payment.created_at),func.sum(Payment.amount),func.count()).where(*filters).group_by(func.date(Payment.created_at)).order_by(func.date(Payment.created_at)))).all()
            return dict(days=days,revenue=total,sales=count,active_keys=active,users=users,daily=[dict(date=r[0],amount=r[1],count=r[2]) for r in daily])
    def search_user(q):
        from app.services.accounts import freeman_id_aliases
        lookup=q.strip().lstrip('@')
        aliases=freeman_id_aliases(lookup) if not lookup.isdigit() else ()
        term='%'+lookup.replace('%','\\%').replace('_','\\_')+'%'
        return or_(User.first_name.ilike(term,escape='\\'),User.username.ilike(term,escape='\\'),User.email.ilike(term,escape='\\'),User.freeman_id.ilike(term,escape='\\'),User.freeman_id.in_(aliases),User.telegram_id==int(lookup) if lookup.isdigit() else False,User.id==int(lookup) if lookup.isdigit() else False)
    def status_filters(model,status):
        if status=='active':
            from app.web.key_availability import active_key
            return [active_key()] if model is AccessKey else [model.status=='active',model.expires_at>now()]
        if status=='expired':return [model.expires_at<=now()]
        if status in ('blocked','frozen'):return [model.status.in_(('blocked','billing_blocked','frozen','disabled'))]
        if status=='soon':return [*status_filters(model,'active'),model.expires_at<=now()+timedelta(days=7)]
        if status!='all':raise HTTPException(422,'Недопустимый статус')
        return []
    @router.get('/api/web/admin/users')
    async def users(q:str='',page:int=1,limit:int=10,status:str='all',origin:str='all',freeman_session:str|None=Cookie(default=None)):
        return await search_users(AdminQuery(q=q,page=page,limit=limit,status=status,origin=origin),freeman_session)
    @router.get('/api/web/admin/users/{user_id}')
    async def user_detail(user_id:int,freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session)
        async with database.sessions() as session:
            u=await session.get(User,user_id)
            if not u:raise HTTPException(404,'Пользователь не найден')
            rows=(await session.execute(select(AccessKey,User).join(User,User.id==AccessKey.user_id).where(AccessKey.user_id==user_id,AccessKey.status!='deleted').order_by(AccessKey.id.desc()))).all()
            proxies=(await session.execute(select(ProxyCredential,ProxyServer).join(ProxyServer,ProxyServer.id==ProxyCredential.server_id).where(ProxyCredential.user_id==user_id,ProxyCredential.status!='deleted').order_by(ProxyCredential.id.desc()))).all()
            from app.services.proxies import traffic
            tickets=list(await session.scalars(select(Ticket).where(Ticket.user_id==user_id).order_by(Ticket.updated_at.desc(),Ticket.id.desc())))
            payments=list(await session.scalars(select(Payment).where(Payment.user_id==user_id).order_by(Payment.created_at.desc(),Payment.id.desc()).limit(100)))
            return dict(tickets=[dict(id=t.id,status=t.status,updated_at=t.updated_at.isoformat()) for t in tickets],user={**user_json(u),'avatar':f'/api/web/admin/users/{u.id}/avatar' if u.telegram_id else None},balance=await SQLRepository(session).balance(user_id),keys=await key_rows(session,rows),proxies=[dict(id=c.id,server=s.name,status=c.status,created_at=c.created_at.isoformat(),expires_at=c.expires_at.isoformat(),traffic=traffic(c.total_octets)) for c,s in proxies],payments=[dict(id=p.id,created_at=p.created_at.isoformat(),amount=p.amount,status=p.status,subscription_id=p.subscription_id) for p in payments])
    from app.services.avatar import TelegramAvatars
    avatars=TelegramAvatars()
    @router.get('/api/web/admin/users/{user_id}/avatar')
    async def avatar(user_id:int,freeman_session:str|None=Cookie(default=None)):
        actor=await current_user(freeman_session)
        if not settings.can_admin_action(actor.telegram_id,'admin:t:home'):raise HTTPException(403,'Нет доступа')
        async with database.sessions() as session:u=await session.get(User,user_id)
        if not u:raise HTTPException(404,'Клиент не найден')
        image=await avatars.get(u.telegram_id,settings.bot_token) if u.telegram_id else None
        return Response(image or b'',status_code=200 if image else 404,media_type='image/jpeg',headers={'Cache-Control':'private, max-age=300' if image else 'no-store'})
    @router.get('/api/web/admin/keys')
    async def keys(q:str='',status:str='all',page:int=1,limit:int=10,origin:str='all',devices:str='all',expiry:int=0,tariff_id:int|None=None,server_id:int|None=None,customer_state:str='all',expired_range:str='all',device_limit:int|None=None,freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session)
        if page<1 or limit not in (10,25,50,100) or len(q)>4096:raise HTTPException(422,'Недопустимый список')
        filters=[AccessKey.status!='deleted',*status_filters(AccessKey,status)]
        linked=select(Subscription.id).join(VlessCredential,VlessCredential.subscription_id==Subscription.id).where(VlessCredential.access_key_id==AccessKey.id).correlate(AccessKey)
        legacy=select(Subscription.id).join(VpnCredential,VpnCredential.subscription_id==Subscription.id).where(VpnCredential.access_key_id==AccessKey.id).correlate(AccessKey)
        sub_ids=linked.union(legacy)
        if q.strip():
            lookup=q.strip().lstrip('@#');term='%'+lookup.replace('%','\\%').replace('_','\\_')+'%'
            matches=[search_user(q),AccessKey.name.ilike(term,escape='\\'),cast(AccessKey.id,String).ilike(term),AccessKey.status.ilike(term),User.freeman_id.ilike(term),cast(User.id,String).ilike(term)]
            from app.database.models import AppSetting
            matches.append(select(AppSetting.key).where(AppSetting.key.in_(select(func.concat('subscription_name:',cast(Subscription.id,String))).where(Subscription.id.in_(sub_ids))),AppSetting.value.ilike(term,escape='\\')).exists())
            matches.append(select(VlessCredential.id).join(RemnawaveServer,RemnawaveServer.id==VlessCredential.server_id).where(VlessCredential.access_key_id==AccessKey.id,or_(VlessCredential.subscription_url.ilike(term,escape='\\'),RemnawaveServer.name.ilike(term),RemnawaveServer.api_url.ilike(term),cast(VlessCredential.subscription_id,String).ilike(term),cast(VlessCredential.id,String).ilike(term))).exists())
            matches.append(select(Subscription.id).outerjoin(Tariff,Tariff.id==Subscription.tariff_id).where(Subscription.id.in_(sub_ids),or_(cast(Subscription.device_limit,String)==lookup,cast(Tariff.months,String)==lookup,func.concat(Tariff.months,' месяц').ilike(term))).exists())
            search_index=len(filters)
            filters.append(or_(*matches))

        if devices not in ('all','none','connected','available','partial','limit','unknown'):raise HTTPException(422,'Неизвестный фильтр устройств')
        if expiry not in (0,1,3,7,30,-1):raise HTTPException(422,'Неизвестный срок')
        if expiry==-1:filters.append(AccessKey.expires_at<=now())
        elif expiry:filters.extend([AccessKey.expires_at>now(),AccessKey.expires_at<=now()+timedelta(days=expiry)])
        count_devices=select(func.count(func.distinct(func.coalesce(Device.provisioning_id,func.concat('local:',Device.id))))).select_from(Device).where(Device.subscription_id.in_(sub_ids),Device.status.in_(('active','connected','online'))).scalar_subquery()
        # Match the authoritative cached HWID semantics used by the device dialog.
        # An unobserved VLESS key is unknown, never an unused subscription.
        from app.database.models import AppSetting
        has_vless=select(VlessCredential.id).where(VlessCredential.access_key_id==AccessKey.id,VlessCredential.status!='deleted').exists()
        observed=or_(select(Device.id).where(Device.subscription_id.in_(sub_ids)).exists(),select(AppSetting.key).where(AppSetting.key.in_(select(func.concat('vless_hwid_snapshot:',cast(Subscription.id,String))).where(Subscription.id.in_(sub_ids)))).exists())
        known=or_(AccessKey.mode=='mock',and_(has_vless,observed))
        capacity=select(Subscription.device_limit).where(Subscription.id.in_(sub_ids)).limit(1).scalar_subquery()
        if devices=='none':filters.extend([known,count_devices==0])
        if devices=='connected':filters.extend([known,count_devices>0])
        if devices=='available':filters.extend([known,or_(capacity==0,count_devices<capacity)])
        if devices=='partial':filters.extend([known,count_devices>0,or_(capacity==0,capacity>count_devices)])
        if devices=='limit':filters.extend([known,capacity>0,count_devices>=capacity])
        if devices=='unknown':filters.append(~known)
        if tariff_id:filters.append(select(Subscription.id).where(Subscription.id.in_(sub_ids),Subscription.tariff_id==tariff_id).exists())
        if server_id:filters.append(select(VlessCredential.id).where(VlessCredential.access_key_id==AccessKey.id,VlessCredential.server_id==server_id).exists())
        if customer_state not in ('all','active','inactive'):raise HTTPException(422,'Неизвестный статус клиента')
        if expired_range not in ('all','7-30','30-90','90+'):raise HTTPException(422,'Неизвестный период истечения')
        if expired_range!='all':
            start,end={'7-30':(7,30),'30-90':(30,90),'90+':(90,None)}[expired_range]
            filters.append(AccessKey.expires_at<=now()-timedelta(days=start))
            if end:filters.append(AccessKey.expires_at>now()-timedelta(days=end))
        if device_limit:filters.append(select(Subscription.id).where(Subscription.id.in_(sub_ids),Subscription.device_limit==device_limit).exists())
        async with database.sessions() as session:
            from app.services.external_contacts import find
            contact_id=await find(SQLRepository(session),settings,q) if q.strip() else None
            if contact_id and q.strip():filters[search_index]=or_(filters[search_index],User.id==contact_id)
            if origin not in ('all','external','internal'):raise HTTPException(422,'Неизвестный источник клиента')
            if origin=='external':filters.append(User.registration_source=='external')
            if origin=='internal':filters.append(User.registration_source!='external')
            if customer_state!='all':
                from app.services.offers import active_customers_query
                ids=active_customers_query()
                filters.append(User.id.in_(ids) if customer_state=='active' else ~User.id.in_(ids))
            count=await session.scalar(select(func.count()).select_from(AccessKey).join(User,User.id==AccessKey.user_id).where(*filters))
            page=min(page,max(1,(count+limit-1)//limit))
            rows=(await session.execute(select(AccessKey,User).join(User,User.id==AccessKey.user_id).where(*filters).order_by(AccessKey.id.desc()).offset((page-1)*limit).limit(limit))).all()
            servers=list(await session.scalars(select(VpnServer).where(VpnServer.status=='active')))
            panels=list(await session.scalars(select(RemnawaveServer).where(RemnawaveServer.status=='active').order_by(RemnawaveServer.priority)))
            tariffs=list(await session.scalars(select(Tariff).order_by(Tariff.months,Tariff.device_limit)))
            summary={}
            for category in ('all','active','soon','expired','blocked'):
                summary[category]=await session.scalar(select(func.count()).select_from(AccessKey).where(AccessKey.status!='deleted',*status_filters(AccessKey,category)))
            return dict(summary=summary,device_limits=sorted(set(t.device_limit for t in tariffs)|set(await session.scalars(select(Subscription.device_limit).distinct()))),tariffs=[dict(id=t.id,name=f'{t.months} мес. · {t.device_limit} устр.',months=t.months,device_limit=t.device_limit) for t in tariffs],items=await key_rows(session,rows),total=count,page=page,limit=limit,q='' if contact_id else q,status=status,servers=[dict(id=p.id,name=p.name,protocol='vless',kind='primary',health=p.health,users_total=p.users_total if p.last_sync else None,users_online=p.users_online if p.last_sync else None,max_users=p.max_users,traffic_used=p.traffic_used if p.last_sync else None,last_sync=p.last_sync.isoformat() if p.last_sync else None) for p in panels]+[dict(id=s.id,name=s.name,protocol=s.protocol,kind='alternative') for s in servers])
    @router.post('/api/web/admin/keys/grant')
    async def grant(payload:VpnGrant,freeman_session:str|None=Cookie(default=None)):
        actor=await allowed(freeman_session)
        if not settings.is_owner(actor.telegram_id):raise HTTPException(403,'Только владелец выдаёт ключи')
        if payload.external_phone and (payload.identity or payload.user_id):raise HTTPException(422,'Укажите одного получателя: Freeman ID или телефон')
        from app.services import manual_sales
        from sqlalchemy import update
        from sqlalchemy.exc import SQLAlchemyError
        from app.services.remnawave import RemnawaveApiError
        try:
            # Durable accounting/terms precede the external call. No money is
            # debited and no payment provider is contacted by this operation.
            async with database.sessions.begin() as session:
                repo=SQLRepository(session)
                await session.execute(update(User).where(User.id==actor.id).values(status=User.status))
                from app.services.accounts import find_freeman_id
                from app.services.external_contacts import create
                if payload.external_phone:u=await create(repo,settings,payload.external_phone)
                elif payload.identity:u=await find_freeman_id(session,payload.identity)
                else:u=await session.get(User,payload.user_id) if payload.user_id else None
                if not u:raise HTTPException(409,'Клиент недоступен')
                saved=await repo.setting('web_vpn_grant:'+payload.request_id)
                if saved:
                    existing=await session.get(AccessKey,int(saved))
                    if not existing or existing.user_id!=u.id:raise HTTPException(409,'Идентификатор выдачи уже использован')
                    old=await manual_sales.read(repo,actor.id,payload.request_id)
                    if old:
                        request=dict(request_id=payload.request_id,issuance_kind=payload.issuance_kind,tariff_id=payload.tariff_id,days=payload.days,devices=payload.device_limit,name=payload.name.strip(),payment_received=payload.payment_received,server_id=payload.server_id,expected_amount=payload.expected_amount)
                        if old.get('legacy_free'):request.update(issuance_kind='complimentary',tariff_id=None,payment_received=False,expected_amount=None)
                        await manual_sales.prepare(repo,actor,u,request)
                        return dict(old['result'],freeman_id=u.freeman_id)
                    return dict(user_id=u.id,key_id=int(saved),freeman_id=u.freeman_id)
                if u.status=='blocked':raise HTTPException(409,'Клиент недоступен')
                old=await manual_sales.read(repo,actor.id,payload.request_id)
                panel=None;server=None
                if old:
                    chosen=old['server']
                    model=RemnawaveServer if chosen['kind']=='vless' else VpnServer
                    server=await session.get(model,chosen['id']);panel=server if chosen['kind']=='vless' else None
                else:
                    panel=await session.scalar(select(RemnawaveServer).where(RemnawaveServer.status=='active').order_by(RemnawaveServer.priority,RemnawaveServer.id).limit(1)) if not payload.server_id else None
                    server=panel or (await session.get(VpnServer,payload.server_id) if payload.server_id else await session.scalar(select(VpnServer).where(VpnServer.status=='active').order_by(VpnServer.priority,VpnServer.id).limit(1)))
                if not server or (not old and server.status!='active'):raise HTTPException(409,'Сначала подключите рабочий VPN-сервер; выдача не выполнена')
                request=dict(request_id=payload.request_id,issuance_kind=payload.issuance_kind,tariff_id=payload.tariff_id,days=payload.days,devices=payload.device_limit,name=payload.name.strip(),payment_received=payload.payment_received,server_id=payload.server_id,expected_amount=payload.expected_amount)
                if old and old.get('legacy_free'):request.update(issuance_kind='complimentary',tariff_id=None,payment_received=False,expected_amount=None)
                legacy=None
                if not old and panel:
                    from app.database.models import VlessCreation
                    from app.services.vless_creation import creation_id,customer_ref
                    legacy=await session.get(VlessCreation,creation_id(await customer_ref(repo,settings,u),'web-grant:'+payload.request_id))
                    if legacy:
                        # A pre-upgrade free grant is never converted to a sale.
                        request.update(issuance_kind='complimentary',tariff_id=None,payment_received=False,expected_amount=None)
                        server=panel=await session.get(RemnawaveServer,legacy.server_id)
                op=await manual_sales.prepare(repo,actor,u,request,server=dict(kind='vless' if panel else 'amnezia',id=server.id))
                if legacy:
                    op['legacy_free']=True
                    op['expires_at']=json.loads(legacy.request)['expireAt']
                    await repo.set_setting(manual_sales.operation_key(actor.id,payload.request_id),json.dumps(op))
            async with database.sessions.begin() as session:
                repo=SQLRepository(session)
                await session.execute(update(User).where(User.id==actor.id).values(status=User.status))
                op=await manual_sales.read(repo,actor.id,payload.request_id)
                if op['result']:return op['result']
                u=await session.get(User,op['request']['user_id'])
                if not u or u.status=='blocked':raise HTTPException(409,'Клиент недоступен')
                terms=op['snapshot'];chosen=op['server']
                server=await session.get(RemnawaveServer if chosen['kind']=='vless' else VpnServer,chosen['id'])
                if not server:raise HTTPException(409,'Исходный сервер недоступен; повторно создавать выдачу не нужно')
                sub=await repo.add(Subscription(user_id=u.id,tariff_id=terms['tariff_id'],kind='paid' if terms['issuance_kind']=='manual_sale' else 'admin',expires_at=datetime.fromisoformat(op['expires_at']),device_limit=terms['devices']))
                await repo.set_setting(f'subscription_traffic_limit:{sub.id}',str(terms['traffic_bytes']))
                if chosen['kind']=='vless':
                    from app.services.vless_provision import provision
                    credential=await provision(repo,settings,sub,u,server,actor,operation_key='web-grant:'+payload.request_id)
                else:
                    from app.services.vpn import VpnService
                    credential=await VpnService(repo,settings).provision(sub,u,server)
                key=await session.get(AccessKey,credential.access_key_id)
                if not key:raise ValueError('Сервер не подтвердил рабочий ключ; результат выдачи проверяется.')
                key.name=op['request']['name'] or f'{now():%d.%m.%Y} · {op["request"]["days"]} дней'
                await repo.set_setting(f'subscription_name:{sub.id}',key.name)
                await repo.set_setting('web_vpn_grant:'+payload.request_id,str(key.id))
                result=await manual_sales.finish(repo,actor,op,sub,key)
                result['freeman_id']=u.freeman_id
                await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='web_vpn_grant',target=str(key.id),details=json.dumps(dict(user_id=u.id,issuance_kind=terms['issuance_kind'],payment_id=op['payment_id'],amount=terms['total']))))
                return result
        except manual_sales.Conflict as error:raise HTTPException(409,str(error)) from error
        except ValueError as error:raise HTTPException(422,str(error)) from error
        except (SQLAlchemyError,RemnawaveApiError,RuntimeError,OSError,TimeoutError) as error:
            raise HTTPException(503,'Результат выдачи пока не подтверждён. Повторите тот же запрос; второй платёж не создаётся.') from error

    @router.get('/api/web/admin/manual-sales/options')
    async def manual_options(freeman_session:str|None=Cookie(default=None)):
        actor=await allowed(freeman_session)
        if not settings.is_owner(actor.telegram_id):raise HTTPException(403,'Только владелец выдаёт ключи')
        async with database.sessions() as session:
            repo=SQLRepository(session);tariffs=[]
            for t in await repo.tariffs():
                if await repo.tariff_setting(t,'tariff_enabled','1')!='1':continue
                tariffs.append(dict(id=t.id,months=t.months,days=max(1,t.months*30),device_limit=t.device_limit,price=t.price,name=await repo.tariff_setting(t,'tariff_name',f'{t.months} мес.')))
            panels=list(await session.scalars(select(RemnawaveServer.id).where(RemnawaveServer.status=='active')))
            servers=list(await session.scalars(select(VpnServer.id).where(VpnServer.status=='active')))
            return dict(tariffs=tariffs,available=bool(panels or servers))

    @router.post('/api/web/admin/manual-sales/quote')
    async def manual_quote(payload:ManualQuote,freeman_session:str|None=Cookie(default=None)):
        actor=await allowed(freeman_session)
        if not settings.is_owner(actor.telegram_id):raise HTTPException(403,'Только владелец выдаёт ключи')
        if payload.external_phone and (payload.identity or payload.user_id):raise HTTPException(422,'Укажите одного получателя: Freeman ID или телефон')
        async with database.sessions() as session:
            repo=SQLRepository(session);sub=None;user=None
            if payload.key_id:
                key,_,_,sub=await key_context(session,payload.key_id)
                user=await session.get(User,key.user_id)
            elif payload.identity:
                from app.services.accounts import find_freeman_id
                user=await find_freeman_id(session,payload.identity)
                if not user:raise HTTPException(422,'Клиент не найден. Укажите Freeman ID')
            elif payload.user_id:
                user=await session.get(User,payload.user_id)
                if not user:raise HTTPException(422,'Клиент не найден')
            elif payload.external_phone:
                from app.services.external_contacts import find,phone
                if not phone(payload.external_phone):raise HTTPException(422,'Укажите полный номер телефона')
                identifier=await find(repo,settings,payload.external_phone)
                user=await session.get(User,identifier) if identifier else None
            tariff=await repo.tariff_by_id(payload.tariff_id)
            if not tariff or await repo.tariff_setting(tariff,'tariff_enabled','1')!='1':raise HTTPException(422,'Тариф недоступен')
            from app.services.billing import purchase_limits
            from app.services.pricing import quote
            from app.services.catalog import public_price
            try:
                await purchase_limits(repo,tariff)
                price=(await quote(repo,user.id,tariff,sub.id if sub else 0))[0] if user else await public_price(repo,'vpn',tariff.id,tariff.price,False)
            except ValueError as error:raise HTTPException(422,str(error)) from error
            return dict(amount=price,tariff_id=tariff.id,days=max(1,tariff.months*30),devices=tariff.device_limit,months=tariff.months)

    @router.get('/api/web/admin/manual-sales/operations/{request_id}')
    async def manual_operation(request_id:str,freeman_session:str|None=Cookie(default=None)):
        actor=await allowed(freeman_session)
        if not settings.is_owner(actor.telegram_id):raise HTTPException(403,'Только владелец управляет выдачей')
        from app.services import manual_sales
        async with database.sessions() as session:
            repo=SQLRepository(session)
            try:op=await manual_sales.read(repo,actor.id,request_id)
            except manual_sales.Conflict as error:raise HTTPException(409,str(error)) from error
            if not op:raise HTTPException(404,'Выдача ещё не зарегистрирована')
            result=op['result']
            if result and op['payment_id']:
                p=await session.get(Payment,op['payment_id'])
                result=dict(result,payment_received=p.status=='manual_paid')
            return dict(request=op['request'],snapshot=op['snapshot'],result=result,freeman_id=op.get('freeman_id'),state='completed' if result else 'prepared')

    @router.post('/api/web/admin/manual-sales/payments/{payment_id}/received')
    async def manual_received(payment_id:int,freeman_session:str|None=Cookie(default=None)):
        actor=await allowed(freeman_session)
        if not settings.is_owner(actor.telegram_id):raise HTTPException(403,'Только владелец подтверждает оплату')
        from app.services import manual_sales
        async with database.sessions.begin() as session:
            repo=SQLRepository(session)
            try:payment=await manual_sales.confirm_received(repo,payment_id,actor)
            except ValueError as error:raise HTTPException(409,str(error)) from error
            return dict(id=payment.id,status=payment.status,amount=payment.amount)

    @router.get('/api/web/admin/keys/{key_id}/export')
    async def export(key_id:int,freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session)
        async with database.sessions() as session:
            key=await session.get(AccessKey,key_id)
            if not key or key.status=='deleted':raise HTTPException(404,'Ключ не найден')
            vpn=await session.scalar(select(VpnCredential).where(VpnCredential.access_key_id==key_id));vless=await session.scalar(select(VlessCredential).where(VlessCredential.access_key_id==key_id))
            if vpn:
                from app.services.vpn import VpnService
                value=VpnService(SQLRepository(session),settings).decrypt(vpn.config);ext='conf'
            elif vless and vless.subscription_url:value=vless.subscription_url;ext='txt'
            else:raise HTTPException(409,'Рабочая конфигурация ещё не выдана; ключ является тестовой записью')
            return Response(value,media_type='text/plain',headers={'Cache-Control':'private, no-store','Content-Disposition':f'attachment; filename="freeman-key-{key_id}.{ext}"'})
    async def key_context(session,key_id):
        key=await session.get(AccessKey,key_id)
        if not key or key.status=='deleted':raise HTTPException(404,'Ключ не найден')
        vpn=await session.scalar(select(VpnCredential).where(VpnCredential.access_key_id==key.id))
        vless=await session.scalar(select(VlessCredential).where(VlessCredential.access_key_id==key.id))
        sub=await session.get(Subscription,(vpn or vless).subscription_id) if vpn or vless else None
        if not sub:
            repo=SQLRepository(session)
            for candidate in await session.scalars(select(Subscription).where(Subscription.user_id==key.user_id)):
                if await repo.setting(f'subscription_key:{candidate.id}')==str(key.id):sub=candidate;break
        return key,vpn,vless,sub
    @router.get('/api/web/admin/keys/{key_id}')
    async def key_detail(key_id:int,freeman_session:str|None=Cookie(default=None)):
        actor=await allowed(freeman_session)
        return await device_key_detail(key_id,actor)
    async def device_key_detail(key_id,actor):
        async with database.sessions.begin() as session:
            remote_meta={}
            key,vpn,vless,sub=await key_context(session,key_id)
            if not settings.is_owner(actor.telegram_id) and key.user_id!=actor.id:raise HTTPException(404,'Ключ не найден')
            u=await session.get(User,key.user_id)
            device_sync=dict(status='unsupported',last_successful_at=None,count=None) if vpn else None
            if vless and sub and vless.status!='deleted':
                from app.services.remnawave import RemnawaveService,RemnawaveApiError
                from app.services.vless_hwid import synchronize,cache_state
                repo=SQLRepository(session)
                service=RemnawaveService(repo,settings)
                try:
                    panel=await service.server(vless.server_id)
                    result=await synchronize(repo,vless,service.api(panel))
                    remote_meta=result or {}
                    sync_status='confirmed' if result is not None else 'superseded'
                except (ValueError,RemnawaveApiError,TimeoutError):
                    sync_status='unavailable'
                cached_count,last_successful=await cache_state(session,sub.id)
                device_sync=dict(status=sync_status,last_successful_at=last_successful,count=cached_count)
            devices=list(await session.scalars(select(Device).where(Device.subscription_id==sub.id))) if sub else []
            payment=await session.scalar(select(Payment).where(Payment.subscription_id==sub.id,Payment.status.in_(PAID)).order_by(Payment.created_at).limit(1)) if sub else None
            buyer=await session.get(User,payment.user_id) if payment else None
            tariff=await session.get(Tariff,sub.tariff_id) if sub and sub.tariff_id else None
            purchase=dict(purchased_at=payment.created_at.isoformat() if payment else None,buyer=user_json(buyer) if buyer else None,amount=payment.amount if payment else None,months=tariff.months if tariff else None,kind=sub.kind if sub else None,first_connected_at=None)

            owner=settings.is_owner(actor.telegram_id)
            provider=bool(vpn or vless);valid=key.status=='active' and key.expires_at.replace(tzinfo=timezone.utc)>now()
            repo=SQLRepository(session)
            from app.services.pricing import quote
            renewal_options=[]
            if owner and provider and sub:
                for option in await repo.tariffs():
                    if await repo.tariff_setting(option, 'tariff_enabled','1')=='1':
                        price,_,_=await quote(repo,u.id,option,sub.id)
                        renewal_options.append(dict(id=option.id,months=option.months,days=option.months*30,price=price,device_limit=option.device_limit))
            server=await session.get(RemnawaveServer,vless.server_id) if vless else await session.get(VpnServer,vpn.server_id) if vpn else None
            device_rows=[]
            for device in devices:
                meta=remote_meta.get(device.provisioning_id,{})
                raw_traffic=meta.get('trafficUsedBytes',meta.get('trafficUsed'))
                traffic_bytes=raw_traffic if isinstance(raw_traffic,int) and raw_traffic>=0 else None
                device_rows.append(dict(traffic_bytes=traffic_bytes,device_identifier=device.provisioning_id,model=meta.get('deviceModel') or meta.get('model'),user_agent=meta.get('userAgent'),id=device.id,name=device_display_name(device.name,meta.get('deviceModel') or meta.get('model')),type=device.type,status=device.status,registered_at=device.created_at.isoformat(),platform=meta.get('platform'),last_activity=meta.get('lastSeenAt') or meta.get('updatedAt'),ip=meta.get('requestIp') or meta.get('ip') or meta.get('ipAddress'),can_unlink=bool(vless and device.provisioning_id)))
            return dict(device_sync=device_sync,purchase=purchase,key=(await key_rows(session,[(key,u)]))[0],user={**user_json(u),'avatar':f'/api/web/admin/users/{u.id}/avatar'},devices=device_rows,device_limit=sub.device_limit if sub else None,last_handshake=vpn.last_handshake.isoformat() if vpn and vpn.last_handshake else None,can_rotate=bool(provider and valid),can_assign=bool(owner and provider and valid and sub),server_name=server.name if server else None,location=server.country if server else None,renewal_options=renewal_options,renewal_mode='admin_extension',actions=dict(delete=bool(owner and provider),unblock=bool(owner and provider and key.expires_at.replace(tzinfo=timezone.utc)>now() and key.status in ('billing_blocked','disabled','blocked')),renew=bool(owner and provider and sub and renewal_options),block=bool(owner and provider and key.status=='active'),restart=bool(owner and provider and valid),copy=bool(vpn or (vless and vless.subscription_url)),devices=bool(sub),unlink_all=bool(owner and sub and (vless or (vpn and key.expires_at.replace(tzinfo=timezone.utc)>now())))),device_note=('Устройства подтверждены панелью Remnawave.' if device_sync and device_sync['status']=='confirmed' else 'Панель временно недоступна или список обновляется. Показаны последние сохранённые устройства.') if vless else 'Amnezia не поддерживает отключение отдельного устройства; сброс всех устройств перевыпускает ключ.')

    @router.get('/api/web/keys/{key_id}/devices')
    async def client_devices(key_id:int,freeman_session:str|None=Cookie(default=None)):
        actor=await current_user(freeman_session)
        detail=await device_key_detail(key_id,actor)
        from app.web.device_limit import LIMIT_REJECTION_STATUS, REGISTERED_DEVICE_STATUSES, device_limit_exceeded
        devices = [d for d in detail['devices'] if d['status'] in REGISTERED_DEVICE_STATUSES]
        rejected = [{'id': d['id'], 'name': d['name'], 'platform': d['platform']}
                    for d in detail['devices'] if d['status'] == LIMIT_REJECTION_STATUS]
        return dict(
            device_sync=detail['device_sync'],
            key={k:detail['key'][k] for k in ('id','name','traffic','expires_at')},
            devices=devices, rejected_devices=rejected, device_limit=detail['device_limit'],
            device_over_limit=device_limit_exceeded(detail['device_limit'], len(devices), len(rejected)),
            actions=dict(unlink_all=any(d['can_unlink'] for d in devices)),
        )

    @router.post('/api/web/keys/{key_id}/devices/reset')
    async def client_reset_devices(key_id:int,payload:KeyAction,freeman_session:str|None=Cookie(default=None)):
        actor=await current_user(freeman_session)
        if not payload.confirmed or payload.action!='reset_devices':raise HTTPException(422,'Подтвердите отвязку устройств')
        async with database.sessions.begin() as session:
            await session.scalar(select(User).where(User.id==actor.id).with_for_update())
            repo=SQLRepository(session)
            request_key=f'client_device_reset:{actor.id}:{key_id}:{payload.request_id}'
            if await repo.setting(request_key)=='done':return dict(ok=True)
            key,vpn,vless,sub=await key_context(session,key_id)
            if key.user_id!=actor.id:raise HTTPException(404,'Ключ не найден')
            if not vless:raise HTTPException(409,'Этот провайдер не поддерживает отвязку устройств')
            try:await reset_devices(session,SQLRepository(session),key_id,actor)
            except ValueError as error:raise HTTPException(409,str(error)) from error
            except Exception as error:raise HTTPException(502,'Панель не подтвердила отвязку устройств') from error
            await repo.set_setting(request_key,'done')
            return dict(ok=True)

    @router.post('/api/web/admin/keys/{key_id}/action')
    async def key_action(key_id:int,payload:KeyAction,freeman_session:str|None=Cookie(default=None)):
        actor=await allowed(freeman_session)
        if not settings.is_owner(actor.telegram_id):raise HTTPException(403,'Изменять ключи может только владелец')
        if payload.action not in ('rename','extend','block','unblock','delete','rotate','reset_devices'):raise HTTPException(422,'Неизвестная операция')
        if not payload.confirmed:raise HTTPException(409,'Подтвердите изменение ключа')
        if payload.issuance_kind is not None:
            if payload.action!='extend':raise HTTPException(422,'Категория выдачи применяется только к продлению')
            from app.services import manual_sales
            from app.services.remnawave import RemnawaveApiError
            from sqlalchemy.exc import SQLAlchemyError
            try:return await manual_sales.renew(database,settings,actor,key_id,payload)
            except manual_sales.Conflict as error:raise HTTPException(409,str(error)) from error
            except ValueError as error:raise HTTPException(409,str(error)) from error
            except (SQLAlchemyError,RemnawaveApiError,RuntimeError,OSError,TimeoutError) as error:
                raise HTTPException(503,'Продление пока не подтверждено. Повторите исходный запрос; новый платёж не создаётся.') from error
        async with database.sessions.begin() as session:
            repo=SQLRepository(session)
            await session.execute(__import__('sqlalchemy').update(User).where(User.id==actor.id).values(status=User.status))
            stamp=f'web_key_action:{actor.id}:{key_id}:'+payload.request_id
            # Preserve pre-upgrade I02 intents, including unfinished renewals.
            fingerprint=hashlib.sha256(json.dumps(payload.model_dump(mode='json',exclude={'issuance_kind','tariff_id','expected_amount'}),sort_keys=True).encode()).hexdigest()
            old=await repo.setting(stamp)
            if old:
                if old!=fingerprint:raise HTTPException(409,'Этот запрос уже использован для другого изменения')
                return dict(key_id=key_id,done=True)
            key,vpn,vless,sub=await key_context(session,key_id)
            if payload.action=='extend' and vless:
                from app.services.offers import lock_customer
                from app.services.vless_renewal import locked_context
                await lock_customer(repo,vless.user_id)
                sub,key,vless=await locked_context(repo,vless)
            if payload.action=='unblock' and (await session.get(User,key.user_id)).status=='blocked':raise HTTPException(409,'Сначала разблокируйте клиента')
            from app.services.vpn import VpnService
            service=VpnService(repo,settings)
            expiry=None;payment=None
            reactivate=bool(payload.action=='extend' and key.status=='billing_blocked' and await repo.setting(f'billing_blocked:{key.id}')=='1')
            if payload.action=='extend':
                base=max(now(),key.expires_at.replace(tzinfo=timezone.utc))
                if payload.unit=='date':
                    if not payload.expires_at:raise HTTPException(422,'Укажите дату окончания')
                    expiry=payload.expires_at.replace(tzinfo=timezone.utc) if payload.expires_at.tzinfo is None else payload.expires_at.astimezone(timezone.utc)
                    if not base<expiry<=now()+timedelta(days=3650):raise HTTPException(422,'Новая дата должна быть позже текущего срока и не более чем на 10 лет вперёд')
                else:
                    units={'hours':1,'days':24,'weeks':168,'months':720}
                    if payload.unit not in units or not payload.value.isdigit():raise HTTPException(422,'Введите целое положительное значение срока')
                    hours=int(payload.value)*units[payload.unit]
                    if not 1<=hours<=87600:raise HTTPException(422,'Срок должен быть от 1 часа до 10 лет')
                    expiry=base+timedelta(hours=hours)
                if payload.payment_mode not in ('free','balance','manual'):raise HTTPException(422,'Неизвестный способ оплаты')
                if payload.payment_mode=='manual' and not payload.payment_confirmed:raise HTTPException(422,'Подтвердите фактическое получение оплаты')
                if payload.payment_mode!='free':
                    if payload.amount<1 or not sub:raise HTTPException(422,'Укажите сумму и связанную подписку для оплаченного продления')
                    await session.execute(__import__('sqlalchemy').update(User).where(User.id==key.user_id).values(status=User.status))
                    if payload.payment_mode=='balance' and await repo.balance(key.user_id)<payload.amount:raise HTTPException(409,'Недостаточно средств на балансе клиента; срок не изменён')
                elif payload.amount:raise HTTPException(422,'При продлении без оплаты сумма должна быть нулевой')
            try:
                if payload.action=='reset_devices':await reset_devices(session,repo,key.id,actor)
                elif payload.action=='rename':
                    if not payload.value.strip():raise ValueError('Введите название')
                    key.name=payload.value.strip()
                    if sub:await repo.set_setting(f'subscription_name:{sub.id}',key.name)
                elif payload.action=='rotate':
                    if not (vpn or vless) or key.status!='active' or key.expires_at.replace(tzinfo=timezone.utc)<=now():raise ValueError('Перевыпуск доступен только для действующего VPN-подключения')
                    if vless:
                        from app.services.vless_provision import control
                        await control(repo,settings,vless,'rotate')
                    else:await service.rotate(sub,await session.get(User,key.user_id))
                else:
                    if vless:
                        from app.services.vless_provision import control
                        remote_expiry=None
                        if payload.action=='extend':
                            remote_expiry=expiry
                        if payload.action=='unblock' and key.expires_at.replace(tzinfo=timezone.utc)<=now():raise ValueError('Сначала продлите истёкший ключ')
                        if payload.action=='extend':
                            result=await control(repo,settings,vless,'extend',remote_expiry,renewal=dict(operation_key=stamp,request=dict(admin=fingerprint),devices=vless.device_limit,activate=key.status=='active' or reactivate))
                            if result:expiry=result.expires_at
                        else:await control(repo,settings,vless,payload.action,remote_expiry)
                    if key.mode!='mock' and not (vpn or vless):raise ValueError('Не найдена связь с VPN-сервером; изменение доступа не выполнено')
                    if payload.action=='extend':
                        if reactivate:
                            key.status='active'
                            if vless:vless.status='active'
                            await repo.set_setting(f'billing_blocked:{key.id}','0')
                            if sub:await repo.set_setting(f'subscription_status:{sub.id}','active')
                        key.expires_at=expiry
                        if sub:sub.expires_at=expiry
                        if vless:vless.expires_at=expiry
                        if vpn:vpn.expires_at=expiry;await service.set_subscription_status(sub.id,'active' if key.status=='active' else 'disabled')
                    else:
                        status={'block':'billing_blocked','unblock':'active','delete':'deleted'}[payload.action]
                        if status=='active' and key.expires_at.replace(tzinfo=timezone.utc)<=now():raise ValueError('Сначала продлите истёкший ключ')
                        if vpn:await service.set_subscription_status(sub.id,'deleted' if status=='deleted' else 'active' if status=='active' else 'disabled')
                        key.status=status
                        if payload.action=='block':
                            await repo.set_setting(f'billing_blocked:{key.id}','0')
                            await repo.set_setting(f'customer_block_key:{key.user_id}:{key.id}','')
                        if vless:vless.status=status
                        if sub:
                            await repo.set_setting(f'subscription_status:{sub.id}',status)
                            if status=='deleted':await repo.set_setting(f'subscription_removed:{sub.id}','1');await repo.set_setting(f'auto_renew:{sub.id}','0')
                        await repo.set_setting(f'client_frozen:{key.id}','0');await repo.set_setting(f'client_freeze_until:{key.id}','')
            except ValueError as e:raise HTTPException(409,str(e)) from e
            if payload.action=='extend' and payload.payment_mode!='free':
                if payload.payment_mode=='balance':
                    await repo.add(BalanceTransaction(user_id=key.user_id,type='subscription_purchase',amount=-payload.amount,description=f'Продление #{sub.id} · {payload.value} {payload.unit}'))
                payment=await repo.add(Payment(user_id=key.user_id,subscription_id=sub.id,amount=payload.amount,status='balance_paid' if payload.payment_mode=='balance' else 'manual_paid',provider_id='admin_extend:'+payload.request_id))
                from app.web.admin_reports import record_sale
                # Relative VLESS retries retain the purchased duration even
                # when recovery uses an earlier, durably frozen target expiry.
                if not vless or payload.unit=='date':hours=int((expiry-base).total_seconds()//3600)
                await record_sale(repo,payment,hours//720 if hours%720==0 else 0,sub.device_limit,renewal=True)
                snapshot=json.loads(await repo.setting(f'sale_snapshot:{payment.id}'));snapshot.update(duration_hours=hours,payment_mode=payload.payment_mode,recorded_by=actor.id)
                await repo.set_setting(f'sale_snapshot:{payment.id}',json.dumps(snapshot))
            await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='web_key_'+payload.action,target=str(key.id),details=json.dumps(dict(value=payload.value,unit=payload.unit,payment_mode=payload.payment_mode,amount=payload.amount,expires_at=expiry.isoformat() if expiry else None,payment_id=payment.id if payment else None))))
            await repo.set_setting(stamp,fingerprint)
            return dict(key_id=key.id,done=True)
    @router.post('/api/web/admin/users/search')
    async def search_users(payload:AdminQuery,freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session)
        if payload.limit not in (10,25,50,100) or payload.page<1:raise HTTPException(422,'Некорректная страница')
        from app.services.client_registry import listing,query
        async with database.sessions() as session:
            try:
                if payload.export:
                    import csv,io
                    stmt=await query(session,settings,payload)
                    if payload.selected_ids:stmt=stmt.where(User.id.in_(payload.selected_ids))
                    out=io.StringIO();writer=csv.writer(out);writer.writerow(['Freeman ID','Клиент','Активные ключи','Активные Proxy','Статус','Последняя активность','Ближайший срок'])
                    async for row in await session.stream(stmt):
                        u,ac,total,pc,seen,nearest,expired,status=row
                        safe=lambda v:"'"+str(v) if str(v).lstrip(' \t\r\n').startswith(('=','+','-','@')) else v
                        writer.writerow([safe(u.freeman_id or ''),safe(u.display_name),ac,pc,status,datetime.fromtimestamp(seen,timezone.utc).isoformat() if seen else '',nearest.isoformat() if nearest else ''])
                    return Response(('\ufeff'+out.getvalue()).encode(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="clients.csv"'})
                return await listing(session,settings,payload)
            except ValueError as e:raise HTTPException(422,str(e)) from e
    @router.post('/api/web/admin/keys/search')
    async def search_keys(payload:AdminQuery,freeman_session:str|None=Cookie(default=None)):
        return await keys(q=payload.q,page=payload.page,limit=payload.limit,status=payload.status,origin=payload.origin,devices=payload.devices,expiry=payload.expiry,tariff_id=payload.tariff_id,server_id=payload.server_id,customer_state=payload.customer_state,expired_range=payload.expired_range,device_limit=payload.device_limit,freeman_session=freeman_session)
    async def reset_devices(session,repo,key_id,actor):
        key,vpn,vless,sub=await key_context(session,key_id)
        if not sub:raise ValueError('У ключа нет связанной подписки')
        if vless:
            from app.services.remnawave import RemnawaveService
            server=await session.get(RemnawaveServer,vless.server_id)
            api=RemnawaveService(repo,settings).api(server)
            await api.delete_all_devices(vless.remote_user_uuid)
            from app.services.vless_hwid import confirmed_removal
            await confirmed_removal(repo,vless,status='removed')
        elif vpn:
            from app.services.vpn import VpnService
            if key.expires_at.replace(tzinfo=timezone.utc)<=now():raise ValueError('Сначала продлите ключ')
            await VpnService(repo,settings).rotate(sub,await session.get(User,key.user_id))
            if key.status!='active':await VpnService(repo,settings).set_subscription_status(sub.id,'disabled')
        elif key.mode!='mock':raise ValueError('Серверный адаптер ключа не найден')
        for d in await session.scalars(select(Device).where(Device.subscription_id==sub.id)):
            d.status='removed'
        await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id or -actor.id,action='web_reset_devices',target=str(key_id),details=f'Freeman ID: {actor.id}; сброшены привязки устройств; ключ не разблокируется автоматически'))
    @router.post('/api/web/admin/keys/reset-blocked-devices')
    async def reset_blocked(payload:KeyAction,freeman_session:str|None=Cookie(default=None)):
        actor=await allowed(freeman_session)
        if not settings.is_owner(actor.telegram_id) or not payload.confirmed:raise HTTPException(403,'Нужно подтверждение владельца')
        async with database.sessions() as session:
            rows=(await session.execute(select(AccessKey,User).join(User,User.id==AccessKey.user_id).where(AccessKey.status!='deleted'))).all()
            candidates=[k['id'] for k in await key_rows(session,rows) if k['device_over_limit'] or (payload.action=='reset_all_devices' and k['mode']!='mock' and k['device_limit'] is not None)]
        done=[];failed=[]
        for kid in candidates:
            try:
                async with database.sessions.begin() as session:
                    repo=SQLRepository(session);stamp='device_reset:'+payload.request_id+':'+str(kid)
                    if not await repo.setting(stamp):await reset_devices(session,repo,kid,actor);await repo.set_setting(stamp,'1')
                done.append(kid)
            except Exception:failed.append(kid)
        return dict(reset=done,failed=failed)
    return router
