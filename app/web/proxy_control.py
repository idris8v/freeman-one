"""Authenticated operational Proxy API. Writes are audited and confirmed."""
from app.web.proxy_renewal import can_renew_proxy
from app.web.access import require_business_admin
import json,re,hashlib,secrets
from datetime import timedelta
from urllib.parse import urlparse
from decimal import Decimal,InvalidOperation
from fastapi import APIRouter,Cookie,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select,func
from app.database.models import *
from app.database.repositories import SQLRepository
from app.services.proxies import ProxyService
from app.services.telemt import TelemtClient,TelemtError
from app.services import proxy_control as domain
from app.services.offers import utc

class Query(BaseModel):
    tab:str=Field(default='servers',pattern='^(servers|proxies|clients|statistics)$')
    q:str=Field(default='',max_length=120)
    page:int=Field(default=1,ge=1,le=100000)
    limit:int=Field(default=10,ge=1,le=100)
    status:str='all'
    server_id:int=0
    source:str='all'
    capacity:str='all'
    country:str=''
    provider:str=''
    issuance:str='all'
    days:int=Field(default=30,ge=1,le=3660)
    user_id:int=0
    start:str=''
    end:str=''
    sort:str=Field(default='id',pattern='^(id|name|active|traffic|expires)$')
    direction:str=Field(default='desc',pattern='^(asc|desc)$')
class ServerInput(BaseModel):
    id:int=0
    name:str=Field(min_length=1,max_length=80)
    api_url:str=Field(min_length=8,max_length=512)
    authorization:str=Field(default='',max_length=2000)
    country:str=Field(default='',max_length=10)
    city:str=Field(default='',max_length=80)
    provider:str=Field(default='',max_length=80)
    notes:str=Field(default='',max_length=500)
    host:str=Field(default='',max_length=255)
    port:int=Field(default=443,ge=1,le=65535)
    audience:str=Field(default='all',pattern='^(all|vpn|paid)$')
    priority:int=Field(default=100,ge=1,le=999)
    ip_limit:int=Field(default=2,ge=1,le=100)
    tcp_limit:int=Field(default=10,ge=1,le=1000)
    max_accounts:int=Field(default=10000,ge=1,le=1000000)
    max_clients:int=Field(default=10000,ge=1,le=1000000)
    traffic_limit:int=Field(default=0,ge=0)
    enabled:bool=True
class Action(BaseModel):
    action:str
    confirmed:bool=False
    request_id:str=Field(default='',max_length=80)
    hours:int=Field(default=720,ge=1,le=87600)
    ip_limit:int=Field(default=2,ge=1,le=100)
    server_id:int=0
class Issue(BaseModel):
    reason:str=Field(default='',max_length=180)
    user_id:int=0
    server_id:int=0
    subscription_id:int=0
    source:str=Field(default='admin',pattern='^(admin|included)$')
    days:int=Field(default=30,ge=1,le=3650)
    ip_limit:int=Field(default=2,ge=1,le=100)
    tcp_limit:int=Field(default=10,ge=1,le=1000)
    invite:bool=False
    for_self:bool=False
    confirmed:bool=False
    request_id:str=Field(min_length=12,max_length=80,pattern='^[A-Za-z0-9_-]+$')
class Purchase(BaseModel):
    method:str=Field(default='balance',pattern='^(balance|yookassa)$')
    plan_id:int
    credential_id:int=Field(default=0,ge=0)
    request_id:str=Field(min_length=12,max_length=80,pattern='^[A-Za-z0-9_-]+$')

async def test_server(repo,settings,p):
    service=ProxyService(repo,settings);parsed=urlparse(p.api_url)
    if parsed.scheme not in ('https','http') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:raise ValueError('Укажите URL API без пароля, query и fragment')
    if parsed.path not in ('','/'):raise ValueError('Укажите базовый URL API без /v1 и других путей')
    if parsed.scheme=='http':
        from ipaddress import ip_address
        try:private=ip_address(parsed.hostname).is_private
        except ValueError:private=False
        if not private:raise ValueError('Для публичного API требуется HTTPS')
    auth=p.authorization
    if p.id and not auth:
        stored=await repo.session.get(ProxyServer,p.id)
        if not stored:raise ValueError('Сервер не найден')
        auth=service.decrypt(stored.auth)
    if not auth:raise ValueError('Укажите Authorization API')
    api=TelemtClient(p.api_url,auth);steps=[]
    try:
        health=await api.request('GET','/v1/health');steps.append(dict(name='API и авторизация',ok=True))
        await api.health();steps.append(dict(name='Telemt готов к подключениям',ok=True))
        await api.users();steps.append(dict(name='Чтение пользователей',ok=True))
        writable=not health.get('read_only',False);steps.append(dict(name='Изменение пользователей разрешено',ok=writable))
        return dict(ok=writable,steps=steps,error='' if writable else 'API работает в read-only; выдача недоступна')
    except (ValueError,TelemtError) as e:
        steps.append(dict(name='Проверка подключения',ok=False));return dict(ok=False,steps=steps,error='Авторизация отклонена' if getattr(e,'status',0) in (401,403) else 'Telemt недоступен или не готов; проверьте URL API, сеть и службу')

async def purchase(repo,settings,user,p):
    from app.services.offers import lock_customer
    await lock_customer(repo,user.id)
    if user.status=='blocked':raise ValueError('Аккаунт заблокирован')
    cfg=await domain.config(repo)
    if not cfg['standalone_enabled']:raise ValueError('Отдельная покупка отключена')
    marker=f'proxy_purchase_v2:{user.id}:{p.request_id}';old=await repo.setting(marker)
    if old:return json.loads(old)
    plan=await repo.lock_catalog_item(ProxyPlan,await repo.session.get(ProxyPlan,p.plan_id),'proxy')
    if not plan or not plan.enabled:raise ValueError('Тариф недоступен')
    c=await repo.session.scalar(select(ProxyCredential).where(ProxyCredential.id==p.credential_id,ProxyCredential.user_id==user.id).with_for_update()) if p.credential_id else None
    if p.credential_id and (not await can_renew_proxy(repo,c)):raise ValueError('Подключение недоступно для продления')
    service=ProxyService(repo,settings);servers=[]
    if c:
        server=await service.server(c.server_id)
        if server.status!='active':raise ValueError('Сервер этого Proxy временно недоступен для продления. Обратитесь в поддержку.')
    else:
        servers=await service.matching_servers('paid')
        if not servers:raise ValueError('Нет доступного сервера; оплата не выполнена')
    price,offer=await domain.quote_proxy(repo,user.id,plan,bool(c))
    order=dict(method=p.method,price=price,base_price=plan.price,days=plan.days,limit=plan.ip_limit,plan_id=plan.id,credential_id=p.credential_id,new_connection=not bool(c),version=2,return_url=settings.web_base_url.rstrip('/')+'/?payment=return')
    if p.method=='yookassa':
        from app.services.proxies import ProxyCheckoutService
        checkout=ProxyCheckoutService(repo,settings)
        if not checkout.enabled or await repo.setting('payment_provider:yookassa','1')!='1':raise ValueError('Оплата картой сейчас недоступна')
        payment=await repo.add(Payment(user_id=user.id,amount=price,status='proxy_order_created'))
        await repo.set_setting(f'proxy_order:{payment.id}',json.dumps(order));await repo.set_setting(f'proxy_order_idem:{payment.id}',p.request_id)
        await domain.reserve_offer(repo,payment,offer,plan.price)
        screen=await checkout.render(f'p:cardpay:{payment.id}',user)
        url=next((b[1][4:] for row in screen.rows for b in row if b[1].startswith('url:https://')),'')
        if not url:raise ValueError('Платёжная система не вернула ссылку')
        result=dict(status='pending',payment_id=payment.id,redirect_url=url,amount=price)
    else:
        if await repo.balance(user.id)<price:raise ValueError('Недостаточно средств на балансе')
        payment=await repo.add(Payment(user_id=user.id,amount=price,status='proxy_order_paid',provider_id='proxy-balance:'+p.request_id))
        expiry=max(now(),utc(c.expires_at)) +timedelta(days=plan.days) if c else now()+timedelta(days=plan.days)
        if c:
            await service.api(await service.server(c.server_id)).patch_user(c.remote_username,expiration_rfc3339=expiry.isoformat().replace('+00:00','Z'),enabled=True,max_unique_ips=plan.ip_limit);c.expires_at=expiry;c.status='active'
        else:c,link=await service.provision(servers[0],user,expiry,plan.ip_limit,cfg['default_tcp_limit'],'paid',connection_token='pay'+str(payment.id))
        await repo.add(BalanceTransaction(user_id=user.id,type='proxy_balance_v2',amount=-price,description=f'Telegram Proxy #{c.id} · {plan.days} дней'))
        await repo.set_setting(f'proxy_order:{payment.id}',json.dumps(order));await domain.reserve_offer(repo,payment,offer,plan.price)
        await domain.record_paid(repo,settings,c,payment,order,bool(p.credential_id))
        result=dict(status='paid',payment_id=payment.id,credential_id=c.id,amount=price)
    await repo.set_setting(marker,json.dumps(result));return result

async def fulfill_v2(repo,settings,payment,order):
    service=ProxyService(repo,settings);user=await repo.session.get(User,payment.user_id)
    c=await repo.session.get(ProxyCredential,order.get('credential_id')) if order.get('credential_id') else None
    if order.get('credential_id') and (not c or c.user_id!=user.id or not await can_renew_proxy(repo,c)):raise ValueError('Выбранный Proxy недоступен. Обратитесь в поддержку.')
    expiry=max(now(),utc(c.expires_at))+timedelta(days=order['days']) if c else now()+timedelta(days=order['days'])
    if c:
        await service.api(await service.server(c.server_id)).patch_user(c.remote_username,expiration_rfc3339=expiry.isoformat().replace('+00:00','Z'),enabled=True,max_unique_ips=order['limit']);c.expires_at=expiry;c.status='active'
    else:
        servers=await service.matching_servers('paid')
        if not servers:raise ValueError('Оплата подтверждена; ожидаем доступность сервера для выдачи')
        c,link=await service.provision(servers[0],user,expiry,order['limit'],(await domain.config(repo))['default_tcp_limit'],'paid',connection_token='pay'+str(payment.id))
    await domain.record_paid(repo,settings,c,payment,order,bool(order.get('credential_id')))
    return c

def proxy_control_router(database,settings,current_user):
    router=APIRouter()
    async def admin(token):
        actor=await current_user(token)
        require_business_admin(actor,settings)
        return actor
    async def audit(repo,actor,action,target):await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id or -actor.id,action='proxy_control_'+action,target=str(target),details='success'))
    @router.post('/api/web/admin/proxy/control/list')
    async def listing(p:Query,freeman_session:str|None=Cookie(default=None)):
        await admin(freeman_session)
        async with database.sessions.begin() as s:
            if p.tab=='clients':
                from app.web.proxy_clients import clients_page
                return await clients_page(SQLRepository(s),settings,p)
            return await domain.page_data(SQLRepository(s),settings,**p.model_dump(exclude={'sort','direction'}))
    @router.post('/api/web/admin/proxy/control/test')
    async def test(p:ServerInput,freeman_session:str|None=Cookie(default=None)):
        await admin(freeman_session)
        async with database.sessions() as s:
            try:return await test_server(SQLRepository(s),settings,p)
            except ValueError as e:raise HTTPException(422,str(e))
    @router.post('/api/web/admin/proxy/control/server')
    async def save_server(p:ServerInput,freeman_session:str|None=Cookie(default=None)):
        actor=await admin(freeman_session)
        async with database.sessions.begin() as s:
            repo=SQLRepository(s);service=ProxyService(repo,settings)
            try:
                result=await test_server(repo,settings,p)
                if not result['ok']:raise ValueError(result['error'])
                duplicate=await s.scalar(select(ProxyServer).where(ProxyServer.api_url==p.api_url.rstrip('/'),ProxyServer.id!=p.id))
                if duplicate:raise ValueError('Такой URL API уже подключён')
                item=await s.get(ProxyServer,p.id) if p.id else None
                if item and item.status=='deleted':raise ValueError('Сервер удалён')
                from app.web.proxy_server_removal import removal_running
                if item and await removal_running(repo,item.id):raise ValueError('Продолжите начатое удаление сервера')
                auth=service.encrypt(p.authorization) if p.authorization else item.auth if item else ''
                if not item:item=await repo.add(ProxyServer(name=p.name,api_url=p.api_url.rstrip('/'),auth=auth))
                item.name=p.name;item.api_url=p.api_url.rstrip('/');item.auth=auth;item.audience=p.audience;item.priority=p.priority;item.unique_ip_limit=p.ip_limit;item.tcp_connection_limit=p.tcp_limit;item.status='active' if p.enabled else 'disabled'
                await repo.set_setting(f'proxy_server_config:{item.id}',json.dumps({k:p.model_dump()[k] for k in ('country','city','provider','notes','host','port','max_accounts','max_clients','traffic_limit')}))
                await service.refresh_server(item);await audit(repo,actor,'server_save',item.id)
                return dict(id=item.id,ok=True)
            except (ValueError,TelemtError) as e:raise HTTPException(409,str(e))
    from app.web.proxy_server_removal import RemovalInput, inspect_removal, remove_batch, removal_running
    @router.get('/api/web/admin/proxy/control/server/{sid}/removal')
    async def removal_preview(sid:int,freeman_session:str|None=Cookie(default=None)):
        await admin(freeman_session)
        async with database.sessions() as s:
            item=await s.get(ProxyServer,sid)
            if not item:raise HTTPException(404,'Сервер не найден')
            return await inspect_removal(SQLRepository(s),item)
    @router.post('/api/web/admin/proxy/control/server/{sid}/removal')
    async def removal_apply(sid:int,p:RemovalInput,freeman_session:str|None=Cookie(default=None)):
        actor=await admin(freeman_session)
        async with database.sessions.begin() as s:
            return await remove_batch(SQLRepository(s),settings,actor,sid,p)
    @router.post('/api/web/admin/proxy/control/server/{sid}/action')
    async def server_action(sid:int,p:Action,freeman_session:str|None=Cookie(default=None)):
        actor=await admin(freeman_session)
        if p.action!='sync' and not p.confirmed:raise HTTPException(409,'Подтвердите действие')
        async with database.sessions.begin() as s:
            repo=SQLRepository(s);service=ProxyService(repo,settings);item=await s.scalar(select(ProxyServer).where(ProxyServer.id==sid).with_for_update())
            if not item or item.status=='deleted':raise HTTPException(404,'Сервер не найден')
            if await removal_running(repo,sid):raise HTTPException(409,'Продолжите начатое удаление сервера')
            try:
                if p.action=='sync':await service.refresh_server(item)
                elif p.action in ('enable','disable','maintenance'):item.status={'enable':'active','disable':'disabled','maintenance':'maintenance'}[p.action]
                elif p.action=='delete':
                    raise ValueError('Откройте удаление сервера с проверкой и отзывом всех подключений')
                elif p.action=='reload':
                    result=await service.api(item).request('POST','/v1/system/reload',{})
                    await audit(repo,actor,'server_reload',sid)
                    return dict(ok=True,reload=result)
                else:raise ValueError('Недопустимое действие')
                await audit(repo,actor,'server_'+p.action,sid);return dict(ok=True,error=item.last_error)
            except (ValueError,TelemtError) as e:raise HTTPException(409,str(e))
    @router.get('/api/web/admin/proxy/control/settings')
    async def settings_get(freeman_session:str|None=Cookie(default=None)):
        await admin(freeman_session)
        async with database.sessions.begin() as s:
            repo=SQLRepository(s)
            return dict(config=await domain.config(repo),plans=[dict(id=p.id,name=p.name,days=p.days,price=p.price,ip_limit=p.ip_limit,enabled=p.enabled) for p in await domain.plans(repo)],tariffs=[dict(id=t.id,months=t.months,devices=t.device_limit) for t in await repo.tariffs()])
    @router.post('/api/web/admin/proxy/control/settings')
    async def settings_save(p:dict,freeman_session:str|None=Cookie(default=None)):
        actor=await admin(freeman_session)
        async with database.sessions.begin() as s:
            repo=SQLRepository(s);cfg=await domain.config(repo)
            for key in ('auto_included','standalone_enabled','notify_activation','notify_expiry'):
                if key in p and not isinstance(p[key],bool):raise HTTPException(422,'Некорректный переключатель')
                if key in p:cfg[key]=p[key]
            for key,lo,hi in [('default_ip_limit',1,100),('default_tcp_limit',1,1000),('grace_days',0,30)]:
                if key in p:
                    try:value=int(p[key])
                    except (ValueError,TypeError):raise HTTPException(422,'Некорректный лимит')
                    if not lo<=value<=hi:raise HTTPException(422,'Лимит вне диапазона')
                    cfg[key]=value
            if 'eligible_tariffs' in p:
                actual={t.id for t in await repo.tariffs()}
                if not isinstance(p['eligible_tariffs'],list) or any(x not in actual for x in p['eligible_tariffs']):raise HTTPException(422,'Неизвестный тариф')
                cfg['eligible_tariffs']=p['eligible_tariffs']
            if 'reminder_days' in p:
                if not isinstance(p['reminder_days'],list) or any(not isinstance(x,int) or x not in (3,1) for x in p['reminder_days']):raise HTTPException(422,'Напоминания доступны за 3 дня и за 1 день')
                cfg['reminder_days']=sorted(set(p['reminder_days']),reverse=True)
            await repo.set_setting('proxy_control_settings',json.dumps(cfg));await audit(repo,actor,'settings','global');return dict(ok=True)
    @router.delete('/api/web/admin/proxy/control/plan/{pid}')
    async def plan_delete(pid:int,version:int,freeman_session:str|None=Cookie(default=None)):
        actor=await admin(freeman_session)
        from app.services.catalog_lifecycle import retire
        async with database.sessions.begin() as s:
            try:result=await retire(SQLRepository(s),'proxy',pid,version)
            except ValueError as error:raise HTTPException(409,str(error)) from error
            if result is None:raise HTTPException(404,'Тариф не найден')
            s.add(AdminAudit(actor_telegram_id=actor.telegram_id or -actor.id,action='catalog_delete',target='proxy:'+str(pid),details=json.dumps(dict(kind='proxy',**result))))
            return result
    @router.post('/api/web/admin/proxy/control/plan')
    async def plan_save(p:dict,freeman_session:str|None=Cookie(default=None)):
        actor=await admin(freeman_session)
        async with database.sessions.begin() as s:
            repo=SQLRepository(s)
            try:
                days=int(p.get('days',30));ip=int(p.get('ip_limit',2));value=Decimal(str(p['price']))
                if not value.is_finite() or value<1 or value>1000000 or value*100!=int(value*100) or not 1<=days<=3650 or not 1<=ip<=100:raise ValueError()
                name=str(p.get('name','Telegram Proxy')).strip()[:80]
                if not name or not isinstance(p.get('enabled',True),bool):raise ValueError()
            except (ValueError,KeyError,TypeError,InvalidOperation):raise HTTPException(422,'Проверьте цену, срок и лимит')
            from app.services.customer_identity import lock_nonce
            if p.get('id'):await lock_nonce(s,'catalog:proxy:'+str(int(p['id'])))
            item=await s.get(ProxyPlan,int(p['id'])) if p.get('id') else await repo.add(ProxyPlan())
            if not item:
                removed=json.loads(await repo.setting(f"catalog_proxy:{p.get('id')}",'{}'))
                raise HTTPException(409 if removed.get('purged') else 404,'Тариф удалён. Обновите список' if removed.get('purged') else 'Тариф не найден')
            metadata=json.loads(await repo.setting(f'catalog_proxy:{item.id}','{}'))
            if metadata.get('deleted'):raise HTTPException(409,'Тариф удалён из каталога')
            if 'version' in p and p['version']!=metadata.get('version',0):raise HTTPException(409,'Тариф изменён. Обновите список')
            before=dict(name=item.name,days=item.days,price=item.price,ip_limit=item.ip_limit,enabled=item.enabled)
            item.name=name;item.days=days;item.price=int(value*100);item.ip_limit=ip;item.enabled=p.get('enabled',True) and not p.get('archived',False)
            await repo.set_setting(f'catalog_proxy:{item.id}',json.dumps(dict(version=metadata.get('version',0)+1,archived=bool(p.get('archived',False)))))
            await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id or -actor.id,action='catalog_change',target=f'proxy:{item.id}',details=json.dumps(dict(kind='proxy',before=before,after=dict(name=name,price=item.price,days=days,devices=ip,enabled=item.enabled)),ensure_ascii=False)))
            if item.id==1:await repo.set_setting('proxy_monthly_price',str(item.price))
            await audit(repo,actor,'plan',item.id);return dict(ok=True,id=item.id)
    @router.get('/api/web/admin/proxy/control/promotions')
    async def promotions(freeman_session:str|None=Cookie(default=None)):
        await admin(freeman_session)
        async with database.sessions() as s:
            rows=await s.execute(select(PersonalOffer.campaign_id,func.max(PersonalOffer.title),func.max(PersonalOffer.expires_at),func.count(PersonalOffer.id),func.sum(PersonalOffer.use_count),func.sum(PersonalOffer.revenue)).where(PersonalOffer.target=='proxy').group_by(PersonalOffer.campaign_id).order_by(PersonalOffer.campaign_id.desc()).limit(100))
            return dict(items=[dict(campaign_id=x[0],title=x[1],expires_at=utc(x[2]).isoformat(),recipients=x[3],redemptions=x[4],revenue=x[5]) for x in rows])
    @router.post('/api/web/admin/proxy/control/issue')
    async def issue(p:Issue,freeman_session:str|None=Cookie(default=None)):
        actor=await admin(freeman_session)
        if not p.confirmed:raise HTTPException(409,'Подтвердите выдачу')
        if p.for_self:
            if p.invite or p.source!='admin':raise HTTPException(422,'Выдача себе доступна только для административного доступа')
            p=p.model_copy(update={'user_id':actor.id})
        async with database.sessions.begin() as s:
            repo=SQLRepository(s);service=ProxyService(repo,settings)
            marker='proxy_issue_v2:'+p.request_id;old=await repo.setting(marker)
            if old:return json.loads(old)
            try:
                servers=await service.matching_servers('vpn' if p.source=='included' else 'paid')
                server=next((v for v in servers if v.id==p.server_id),None) if p.server_id else next(iter(servers),None)
                if not server:raise ValueError('Нет проверенного сервера со свободной ёмкостью')
                if p.invite:
                    if p.source!='admin':raise ValueError('Для включённого доступа выберите клиента и подписку')
                    token=secrets.token_urlsafe(24);key='proxy_invite:'+hashlib.sha256(token.encode()).hexdigest()
                    data=dict(server_id=server.id,days=p.days,ip=p.ip_limit,tcp=p.tcp_limit,actor=actor.telegram_id,expires=(now()+timedelta(days=3)).isoformat(),status='open',source='admin',intended_user=p.user_id or None)
                    await repo.set_setting(key,json.dumps(data));result=dict(invite=True,link=settings.web_base_url.rstrip('/')+'/?proxy_invite='+token,telegram_link=f'https://t.me/{settings.bot_username}?start=px_{token}',expires_at=data['expires'])
                else:
                    user=await s.scalar(select(User).where(User.id==p.user_id).with_for_update())
                    if not user or user.status=='blocked':raise ValueError('Выберите существующий аккаунт клиента')
                    sub=await s.get(Subscription,p.subscription_id) if p.source=='included' else None
                    if p.source=='included' and (not sub or sub.user_id!=user.id or not await domain.subscription_eligible(repo,sub)):raise ValueError('Нужна действующая допустимая подписка этого клиента')
                    expiry=utc(sub.expires_at) if sub else now()+timedelta(days=p.days)
                    c,link=await service.provision(server,user,expiry,p.ip_limit,p.tcp_limit,'vpn' if sub else 'paid',connection_token=hashlib.sha256(p.request_id.encode()).hexdigest()[:20])
                    m=await domain.meta(repo,c);m.source=p.source;m.subscription_id=sub.id if sub else None
                    await domain.notify(repo,settings,user,c,'activated');result=dict(id=c.id,link=link,expires_at=utc(c.expires_at).isoformat())
                await repo.set_setting(marker,json.dumps(result));await audit(repo,actor,'issue',result.get('id','invitation'));await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id or -actor.id,action='proxy_issue_reason',target=str(user.id) if not p.invite else 'invitation',details=p.reason));return result
            except (ValueError,TelemtError) as e:raise HTTPException(409,str(e))
    @router.get('/api/web/admin/proxy/control/invites')
    async def invites(freeman_session:str|None=Cookie(default=None)):
        await admin(freeman_session)
        async with database.sessions() as s:
            rows=[]
            for p in await s.scalars(select(AppSetting).where(AppSetting.key.startswith('proxy_invite:')).order_by(AppSetting.key).limit(100)):
                try:v=json.loads(p.value)
                except ValueError:continue
                rows.append(dict(id=p.key.split(':')[1],server_id=v.get('server_id'),status='expired' if v.get('status')=='open' and v['expires']<now().isoformat() else v.get('status'),expires_at=v.get('expires'),user_id=v.get('user_id'),credential_id=v.get('credential_id')))
            return dict(items=rows)
    @router.post('/api/web/admin/proxy/control/invites/{digest}/revoke')
    async def revoke(digest:str,p:Action,freeman_session:str|None=Cookie(default=None)):
        actor=await admin(freeman_session)
        if not p.confirmed or not re.fullmatch('[a-f0-9]{64}',digest):raise HTTPException(422,'Подтвердите отзыв')
        async with database.sessions.begin() as s:
            repo=SQLRepository(s);raw=await repo.setting('proxy_invite:'+digest)
            if not raw:raise HTTPException(404,'Приглашение не найдено')
            v=json.loads(raw)
            if v['status']!='open':raise HTTPException(409,'Приглашение уже использовано или закрыто')
            v['status']='revoked';await repo.set_setting('proxy_invite:'+digest,json.dumps(v));await audit(repo,actor,'invite_revoke',digest);return dict(ok=True)
    async def detail(s,c):
        repo=SQLRepository(s);service=ProxyService(repo,settings);m=await domain.meta(repo,c);server=await s.get(ProxyServer,c.server_id);u=await s.get(User,c.user_id)
        ips=list(await s.scalars(select(ProxyIPSeen).where(ProxyIPSeen.credential_id==c.id).order_by(ProxyIPSeen.last_seen.desc()).limit(100)))
        return dict(id=c.id,user_id=c.user_id,name=u.display_name,freeman_id=u.freeman_id,server=server.name,server_id=server.id,source=m.source,subscription_id=m.subscription_id,payment_id=m.payment_id,ip_limit=m.ip_limit,ips=[dict(ip=i.ip,first_seen=utc(i.created_at).isoformat(),last_seen=utc(i.last_seen).isoformat(),active=i.active and domain.fresh(server)) for i in ips],ips_supported=await repo.setting(f'proxy_ips_supported:{c.id}')=='1',traffic=c.total_octets if c.last_sync else None,connections=c.current_connections if domain.fresh(server) else None,expires_at=utc(c.expires_at).isoformat(),status=c.status,link=service.decrypt(c.link) if c.link else '',capabilities=dict(reset_all=True,unlink_one=False),ip_note='Telemt предоставляет текущие IP. Отдельный сброс MTProto IP не поддерживается API; сброс всех разрывает соединения через disable/enable.')
    @router.get('/api/web/admin/proxy/control/access/{cid}')
    async def access_detail(cid:int,freeman_session:str|None=Cookie(default=None)):
        await admin(freeman_session)
        async with database.sessions.begin() as s:
            c=await s.get(ProxyCredential,cid)
            if not c:raise HTTPException(404,'Прокси не найден')
            return await detail(s,c)
    @router.post('/api/web/admin/proxy/control/access/{cid}/action')
    async def access_action(cid:int,p:Action,freeman_session:str|None=Cookie(default=None)):
        actor=await admin(freeman_session)
        if not p.confirmed:raise HTTPException(409,'Подтвердите действие')
        async with database.sessions.begin() as s:
            repo=SQLRepository(s);service=ProxyService(repo,settings);c=await s.scalar(select(ProxyCredential).where(ProxyCredential.id==cid).with_for_update())
            if not c or c.status in ('deleted','migrated'):raise HTTPException(404,'Прокси не найден')
            m=await domain.meta(repo,c);api=service.api(await service.server(c.server_id))
            try:
                if p.action=='extend':
                    if m.source=='included':raise ValueError('Продлите основную подписку; включённый Proxy следует её сроку')
                    expiry=max(now(),utc(c.expires_at))+timedelta(hours=p.hours);await api.patch_user(c.remote_username,expiration_rfc3339=expiry.isoformat().replace('+00:00','Z'));c.expires_at=expiry
                elif p.action=='block':
                    await api.disable_user(c.remote_username);c.status='disabled';await repo.set_setting(f'proxy_sub_suspended:{cid}','0');await repo.set_setting(f'customer_block_proxy:{c.user_id}:{cid}','')
                elif p.action=='unblock':
                    if (await s.get(User,c.user_id)).status=='blocked':raise ValueError('Сначала разблокируйте клиента')
                    if utc(c.expires_at)<=now():raise ValueError('Сначала продлите срок')
                    await api.enable_user(c.remote_username);c.status='active'
                elif p.action=='delete':await api.delete_user(c.remote_username);c.status='deleted';c.link=''
                elif p.action=='limit':await api.patch_user(c.remote_username,max_unique_ips=p.ip_limit);m.ip_limit=p.ip_limit;await repo.set_setting(f'proxy_credential_ip_limit:{cid}',str(p.ip_limit))
                elif p.action=='reset':
                    if c.status!='active' or utc(c.expires_at)<=now():raise ValueError('Сброс доступен действующим подключениям')
                    await api.disable_user(c.remote_username);c.status='disabled'
                    # Do not claim a successful reset if re-enable failed.
                    try:await api.enable_user(c.remote_username)
                    except (ValueError,TelemtError):
                        await audit(repo,actor,'reset_enable_failed',cid);return dict(ok=False,error='Соединения остановлены, но повторное включение не удалось. Нажмите «Включить» после восстановления API.')
                    c.status='active';await service.refresh_server(await service.server(c.server_id))
                elif p.action=='rotate':
                    data=await api.request('POST','/v1/users/'+c.remote_username+'/rotate-secret',{});link=service.choose_link(data.get('user',data)) or service.choose_link(await api.user(c.remote_username))
                    if not link:raise ValueError('Telemt не вернул новую ссылку')
                    c.link=service.encrypt(link)
                elif p.action=='move':
                    target=await service.server(p.server_id);new,link,revoked=await service.move_credential(c,target,actor)
                    nm=await domain.meta(repo,new);nm.source=m.source;nm.subscription_id=m.subscription_id;nm.payment_id=m.payment_id;nm.plan_id=m.plan_id
                    return dict(ok=True,id=new.id,warning='' if revoked else 'Новый доступ создан; отзыв старого сервера не подтверждён')
                else:raise ValueError('Недопустимое действие')
                await audit(repo,actor,'access_'+p.action,cid);return dict(ok=True)
            except (ValueError,TelemtError) as e:raise HTTPException(409,str(e))
    @router.get('/api/web/proxy/catalog')
    async def catalog(credential_id:int=0,freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        async with database.sessions.begin() as s:
            repo=SQLRepository(s);cfg=await domain.config(repo);values=[]
            credential=await s.scalar(select(ProxyCredential).where(ProxyCredential.id==credential_id,ProxyCredential.user_id==user.id)) if credential_id else None
            if credential_id and (not await can_renew_proxy(repo,credential)):raise HTTPException(404,'Proxy не найден или недоступен')
            if credential and (await s.get(ProxyServer,credential.server_id)).status!='active':raise HTTPException(409,'Сервер этого Proxy временно недоступен для продления')
            selected=dict(id=credential.id,source=(await domain.meta(repo,credential)).source,expires_at=utc(credential.expires_at).isoformat()) if credential else None
            for p in await domain.plans(repo):
                if p.enabled:
                    price,offer=await domain.quote_proxy(repo,user.id,p,bool(credential));values.append(dict(id=p.id,name=p.name,days=p.days,ip_limit=p.ip_limit,price=price,base_price=p.price))
            return dict(enabled=cfg['standalone_enabled'],credential=selected,plans=values,balance=await repo.balance(user.id),payments=dict(yookassa=bool(settings.yookassa_shop_id and settings.yookassa_secret_key) and await repo.setting('payment_provider:yookassa','1')=='1'))
    @router.post('/api/web/proxy/purchase')
    async def customer_purchase(p:Purchase,freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        async with database.sessions.begin() as s:
            try:return await purchase(SQLRepository(s),settings,user,p)
            except (ValueError,TelemtError) as e:raise HTTPException(409,str(e))
    @router.post('/api/web/proxy/activate')
    async def activate(p:dict,freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        from app.services.proxy_invites import ProxyInvites
        async with database.sessions.begin() as s:
            try:
                screen=await ProxyInvites(SQLRepository(s),settings).claim(str(p.get('token','')),user)
                return dict(ok=True,link=next(b[1][4:] for r in screen.rows for b in r if b[1].startswith('url:')))
            except ValueError as e:raise HTTPException(409,str(e))
    @router.get('/api/web/proxy/access/{cid}')
    async def customer_detail(cid:int,freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        async with database.sessions.begin() as s:
            c=await s.get(ProxyCredential,cid)
            if not c or c.user_id!=user.id:raise HTTPException(404,'Прокси не найден')
            return await detail(s,c)
    @router.post('/api/web/proxy/access/{cid}/reset')
    async def customer_reset(cid:int,p:Action,freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        if not p.confirmed:raise HTTPException(409,'Подтвердите сброс')
        async with database.sessions.begin() as s:
            repo=SQLRepository(s);c=await s.scalar(select(ProxyCredential).where(ProxyCredential.id==cid,ProxyCredential.user_id==user.id).with_for_update())
            if not c or c.status!='active' or utc(c.expires_at)<=now():raise HTTPException(404,'Активный прокси не найден')
            service=ProxyService(repo,settings);server=await service.server(c.server_id)
            try:
                await service.api(server).disable_user(c.remote_username);c.status='disabled'
                try:await service.api(server).enable_user(c.remote_username)
                except (ValueError,TelemtError):return dict(ok=False,error='Доступ остановлен; повторное включение не удалось. Обратитесь в поддержку.')
                c.status='active';await service.refresh_server(server);return dict(ok=True)
            except (ValueError,TelemtError) as e:raise HTTPException(409,str(e))
    return router
