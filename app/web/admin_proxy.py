from app.services.accounts import freeman_id_aliases
from app.web.access import require_business_admin
import re,html
from datetime import timezone,timedelta
from fastapi import APIRouter,Cookie,HTTPException
from fastapi.responses import Response
from pydantic import BaseModel,Field
from sqlalchemy import select,or_,func
from app.database.models import ProxyServer,ProxyCredential,ProxyEndpoint,User,AdminAudit,now
from app.database.repositories import SQLRepository
from app.services.proxies import ProxyService,traffic
class WebProxyService(ProxyService):
    async def move_credential(self,credential,target,actor):
        self._move_token=f'move_{credential.id}'
        try:return await super().move_credential(credential,target,actor)
        finally:self._move_token=None
    async def provision(self,*args,**kwargs):
        if getattr(self,'_move_token',None) and not kwargs.get('connection_token'):kwargs['connection_token']=self._move_token
        return await super().provision(*args,**kwargs)

class ProxyAdminAction(BaseModel):
    action:str=Field(max_length=120)
    text:str=Field(default='',max_length=2500)
    confirmed:bool=False
    request_id:str=Field(default="",max_length=36,pattern=r"^[a-zA-Z0-9-]*$")

class ProxyGrant(BaseModel):
    identity:str=Field(default='',max_length=100)
    server_id:int
    days:int=Field(default=30,ge=1,le=3650)
    ip:int=Field(default=2,ge=2,le=100)
    tcp:int=Field(default=2,ge=2,le=1000)
    request_id:str=Field(min_length=10,max_length=36,pattern=r'^[a-zA-Z0-9-]+$')

def admin_proxy_router(database,settings,current_user):
    router=APIRouter()
    async def allowed(token,owner=False):
        actor=await current_user(token)
        require_business_admin(actor,settings)
        return actor
    def screen_json(screen):
        return dict(tables=[[[str(getattr(cell,'text','')) for cell in row] for row in getattr(block,'cells',[])] for block in getattr(screen.rich,'blocks',[]) if getattr(block,'cells',None)],options=screen.button_options,text=html.unescape(re.sub('<[^>]*>','',screen.text)),rows=[[dict(label=label,action=action) for label,action in row if action.startswith('admin:p:') or action.startswith('url:')] for row in screen.rows])
    async def clients_screen(session,server_id,q='',page=1,limit=10,status='all'):
        if page<1 or limit not in (10,25,50,100) or len(q)>100:raise HTTPException(422,'Недопустимые параметры списка')
        item=await session.get(ProxyServer,server_id)
        if not item or item.status=='deleted':raise HTTPException(404,'Сервер не найден')
        if status not in ('all','active','disabled','expired','migrated'):raise HTTPException(422,'Недопустимый статус')
        filters=[ProxyCredential.server_id==server_id,ProxyCredential.status!='deleted']
        if status=='active':filters.extend([ProxyCredential.status=='active',ProxyCredential.expires_at>now()])
        elif status=='expired':filters.append(ProxyCredential.expires_at<=now())
        elif status!='all':filters.append(ProxyCredential.status==status)
        if q.strip():
            q=q.strip().lstrip('@')
            term='%'+q.strip().replace('%','\\%').replace('_','\\_')+'%'
            filters.append(or_(User.first_name.ilike(term,escape='\\'),User.username.ilike(term,escape='\\'),User.freeman_id.ilike(term,escape='\\'),User.freeman_id.in_(freeman_id_aliases(q)),ProxyCredential.remote_username.ilike(term,escape='\\'),User.telegram_id==int(q) if q.isdigit() else False,ProxyCredential.id==int(q) if q.isdigit() else False))
        base=select(ProxyCredential.user_id).outerjoin(User,User.id==ProxyCredential.user_id).where(*filters).distinct()
        total=await session.scalar(select(func.count()).select_from(base.subquery()))
        page=min(page,max(1,(total+limit-1)//limit))
        ids=list(await session.scalars(base.order_by(ProxyCredential.user_id.desc()).offset((page-1)*limit).limit(limit)))
        rows=(await session.execute(select(ProxyCredential,User).outerjoin(User,User.id==ProxyCredential.user_id).where(*filters,ProxyCredential.user_id.in_(ids)).order_by(ProxyCredential.id.desc()))).all()
        groups={uid:[] for uid in ids}
        for c,u in rows:groups[c.user_id].append((c,u))
        clients=[];repo=SQLRepository(session)
        for uid,items in groups.items():
            c,u=items[0];keys=[r[0] for r in items];active=sum(k.status=='active' and k.expires_at.replace(tzinfo=timezone.utc)>now() for k in keys)
            clients.append(dict(id=c.id,user_id=uid,key_count=len(keys),active_count=active,name=(u.display_name or u.username or str(u.telegram_id)) if u else c.remote_username,username=u.username if u else '',freeman_id=u.freeman_id if u else '',telegram_id=u.telegram_id if u else None,remote_username='',status='active' if active else c.status,created_at=min(k.created_at for k in keys).isoformat(),expires_at=max(k.expires_at for k in keys).isoformat(),connections=sum(k.current_connections for k in keys),ips=sum(k.active_unique_ips for k in keys),traffic=traffic(sum(k.total_octets for k in keys)),last_sync=c.last_sync.isoformat() if c.last_sync else None,kind=await repo.setting(f'proxy_credential_kind:{c.id}','unknown'),ip_limit=await repo.setting(f'proxy_credential_ip_limit:{c.id}','—')))
        return dict(kind='clients',title=item.name+' · клиенты',server_id=server_id,clients=clients,total=total,page=page,limit=limit,q=q,status=status,back=f'admin:p:server:{server_id}',rows=[],tables=[],text='')
    @router.get('/api/web/admin/proxy/clients')
    async def clients(server_id:int,q:str='',page:int=1,limit:int=10,status:str='all',freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session,True)
        async with database.sessions() as session:return await clients_screen(session,server_id,q,page,limit,status)
    @router.get('/api/web/admin/proxy/customer/{user_id}')
    async def customer(user_id:int,freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session,True)
        async with database.sessions() as session:
            u=await session.get(User,user_id)
            if not u:raise HTTPException(404,'Клиент не найден')
            rows=(await session.execute(select(ProxyCredential,ProxyServer).join(ProxyServer,ProxyServer.id==ProxyCredential.server_id).where(ProxyCredential.user_id==user_id,ProxyCredential.status!='deleted').order_by(ProxyCredential.id.desc()))).all()
            repo=SQLRepository(session);keys=[]
            for c,server in rows:
                keys.append(dict(id=c.id,server_id=server.id,server=server.name,status=c.status,created_at=c.created_at.isoformat(),expires_at=c.expires_at.isoformat(),traffic=traffic(c.total_octets),ips=c.active_unique_ips,connections=c.current_connections,kind=await repo.setting(f'proxy_credential_kind:{c.id}','unknown'),ip_limit=await repo.setting(f'proxy_credential_ip_limit:{c.id}','—')))
            targets=list(await session.scalars(select(ProxyServer).where(ProxyServer.status=='active').order_by(ProxyServer.priority)))
            return dict(targets=[dict(id=t.id,name=t.name) for t in targets],kind='customer',title=u.display_name or u.username or 'Клиент',user=dict(id=u.id,name=u.display_name or u.username or 'Клиент',username=u.username,telegram_id=u.telegram_id,freeman_id=u.freeman_id,avatar=f'/api/web/admin/proxy/customer/{u.id}/avatar' if u.telegram_id else None),keys=keys,rows=[],tables=[],text='')
    from app.services.avatar import TelegramAvatars
    avatars=TelegramAvatars()
    @router.get('/api/web/admin/proxy/customer/{user_id}/avatar')
    async def customer_avatar(user_id:int,freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session,True)
        async with database.sessions() as session:u=await session.get(User,user_id)
        if not u:raise HTTPException(404,'Клиент не найден')
        image=await avatars.get(u.telegram_id,settings.bot_token) if u.telegram_id else None
        if not image:raise HTTPException(404,'Аватар не установлен')
        return Response(content=image,media_type='image/jpeg',headers={'Cache-Control':'private, max-age=300'})
    @router.post('/api/web/admin/proxy/grant')
    async def grant(payload:ProxyGrant,freeman_session:str|None=Cookie(default=None)):
        actor=await allowed(freeman_session,True)
        from app.services.proxy_invites import ProxyInvites
        async with database.sessions.begin() as session:
            repo=SQLRepository(session)
            try:
                if not payload.identity.strip():return dict(kind='invite',**await ProxyInvites(repo,settings).create(actor,payload.server_id,payload.days,payload.ip,payload.tcp))
                identity=payload.identity.strip()
                user=await session.scalar(select(User).where(or_(User.freeman_id.in_(freeman_id_aliases(identity)),User.email==identity.lower(),User.telegram_id==int(identity) if identity.isdigit() else False)))
                if not user or not user.telegram_id or user.status=='blocked':raise ValueError('Выберите клиента из подсказок или укажите точный ID / email')
                service=WebProxyService(repo,settings);server=await service.server(payload.server_id);expiry=now()+timedelta(days=payload.days)
                credential,link=await service.provision(server,user,expiry,payload.ip,payload.tcp,'paid',connection_token=payload.request_id.replace('-',''))
                current=await repo.setting(f'proxy_paid_until:{user.id}')
                if not current or current<expiry.isoformat():await repo.set_setting(f'proxy_paid_until:{user.id}',expiry.isoformat())
                await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='web_proxy_grant',target=str(user.id),details=f'credential={credential.id}; server={server.id}; days={payload.days}'))
                return dict(kind='issued',user_id=user.id,credential_id=credential.id)
            except ValueError as error:raise HTTPException(409,str(error)) from error
    @router.get('/api/web/admin/proxy')
    async def proxy(freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session,True)
        async with database.sessions() as session:
            repo=SQLRepository(session)
            rows=await session.scalars(select(ProxyServer).where(ProxyServer.status!='deleted').order_by(ProxyServer.priority))
            legacy=list(await session.scalars(select(ProxyEndpoint).where(ProxyEndpoint.status!='deleted')))
            return dict(legacy=[dict(id=e.id,name=e.name,status=e.status,host=e.host,port=e.port) for e in legacy],price=int(await repo.setting('proxy_monthly_price','3000')),servers=[dict(id=s.id,name=s.name,audience=s.audience,status=s.status,health=s.health,active_users=s.active_users,connections=s.current_connections,traffic=traffic(s.total_octets),error=s.last_error,last_sync=s.last_sync.isoformat() if s.last_sync else None) for s in rows])
    @router.post('/api/web/admin/proxy/action')
    async def action(payload:ProxyAdminAction,freeman_session:str|None=Cookie(default=None)):
        actor=await allowed(freeman_session,True)
        if re.fullmatch(r'admin:p:credextend:[0-9]+',payload.action):
            if not payload.confirmed or not payload.request_id:raise HTTPException(409,'Подтвердите продление')
            if not payload.text.isdigit() or not 1<=int(payload.text)<=3650:raise HTTPException(422,'Укажите от 1 до 3650 дней')
            from app.services.telemt import TelemtError
            async with database.sessions.begin() as session:
                service=WebProxyService(SQLRepository(session),settings)
                credential=await session.scalar(select(ProxyCredential).where(ProxyCredential.id==int(payload.action.split(':')[3])).with_for_update())
                if not credential or credential.status in ('deleted','migrated','missing'):raise HTTPException(409,'Ключ недоступен')
                marker='proxy_extend_request:'+payload.request_id
                if not await service.repo.setting(marker):
                    server=await service.server(credential.server_id)
                    expiry=max(now(),credential.expires_at.replace(tzinfo=timezone.utc))+timedelta(days=int(payload.text))
                    try:await service.api(server).patch_user(credential.remote_username,expiration_rfc3339=expiry.isoformat().replace('+00:00','Z'))
                    except (TelemtError,ValueError) as error:raise HTTPException(409,str(error)) from error
                    credential.expires_at=expiry;credential.last_sync=now()
                    await service.repo.set_setting(marker,str(credential.id))
                    await service.repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='web_proxy_extend',target=str(credential.id),details='days='+payload.text))
                elif await service.repo.setting(marker)!=str(credential.id):raise HTTPException(409,'Операция относится к другому ключу')
            payload.action='admin:p:cred:'+str(credential.id);payload.text=''
        if payload.action=='admin:guide:proxy':
            from app.services.guides import GUIDES
            title,text=GUIDES['proxy']
            steps=[
                dict(title='Установка Telemt',text='На новом Ubuntu / Debian сервере установите Telemt официальным установщиком. Выберите свободный клиентский порт и TLS-домен. Эти команды выполняются на новом прокси-сервере.',code='curl -fsSL https://raw.githubusercontent.com/telemt/telemt/main/install.sh -o /tmp/telemt-install.sh\nsh /tmp/telemt-install.sh',code_label='Терминал нового сервера'),
                dict(title='Ключ управления API',text='Сгенерируйте случайный токен. Сохраните результат: его нужно вставить вместо REPLACE_WITH_RANDOM_TOKEN в конфигурации и в поле Authorization на сайте.',code='openssl rand -hex 32',code_label='Терминал · генерация токена'),
                dict(title='Конфигурация API',text='Добавьте или обновите секции в /etc/telemt/telemt.toml, не создавая повторных TOML-секций. Замените домен прокси и токен. API слушает локально: доступ с сервера бота организуйте через приватную сеть либо HTTPS reverse proxy.',code='[general.links]\npublic_host = "proxy.example.com"\npublic_port = 443\n\n[server.api]\nenabled = true\nlisten = "127.0.0.1:9091"\nwhitelist = ["127.0.0.1/32", "::1/128"]\nauth_header = "REPLACE_WITH_RANDOM_TOKEN"\nruntime_edge_enabled = true\nread_only = false',code_label='telemt.toml · шаблон'),
                dict(title='Применение и проверка',text='Для установки как systemd-служба перезапустите Telemt и проверьте API. После export вставьте свой токен. При Docker перезапускайте сервис Telemt в его compose-проекте; каталог конфигурации должен быть доступен для записи.',code='sudo systemctl restart telemt\nexport TELEMT_API_AUTH="REPLACE_WITH_RANDOM_TOKEN"\ncurl --fail --silent --show-error -H "Authorization: $TELEMT_API_AUTH" http://127.0.0.1:9091/v1/health/ready',code_label='Терминал · systemd'),
                dict(title='Подключение к Freeman',text='Откройте «Подключить сервер». Укажите название, HTTPS URL API либо приватный HTTP IP, группу выдачи, приоритет, лимиты IP / TCP и тот же Authorization. Для HTTPS reverse proxy ограничьте источник адресом сервера бота 45.80.231.168/32. После проверки сервер появится в списке; выполните синхронизацию.',code='Название | https://proxy-api.example.com | all | 100 | 2 | 2 | REPLACE_WITH_RANDOM_TOKEN',code_label='Пример полей мастера')]
            return dict(kind='guide',title=title,steps=steps,text=text,tables=[],rows=[],back='admin:p:home',source='https://github.com/telemt/telemt/blob/main/docs/Architecture/API/API.md')
        if payload.action.startswith('admin:p:users:'):
            parts=payload.action.split(':')
            if len(parts)!=5 or not parts[3].isdigit() or not parts[4].isdigit():raise HTTPException(422,'Недопустимый список')
            async with database.sessions() as session:return await clients_screen(session,int(parts[3]))
        if re.fullmatch(r'admin:p:customermove:[0-9]+:[0-9]+',payload.action):
            if not payload.confirmed:raise HTTPException(409,'Подтвердите перенос ключей клиента')
            user_id,target_id=map(int,payload.action.split(':')[3:5]);moved=failed=skipped=0;errors=[]
            async with database.sessions.begin() as session:
                service=WebProxyService(SQLRepository(session),settings);target=await service.server(target_id)
                keys=list(await session.scalars(select(ProxyCredential).where(ProxyCredential.user_id==user_id,ProxyCredential.status=='active',ProxyCredential.expires_at>now())))
                if not keys:raise HTTPException(409,'Нет активных ключей для переноса')
                for key in keys:
                    if key.server_id==target_id:skipped+=1;continue
                    try:
                        new,link,revoked=await service.move_credential(key,target,actor)
                        moved+=1
                        if not revoked:errors.append(f'Ключ #{key.id}: старый доступ не удалось отозвать')
                    except ValueError as error:failed+=1;errors.append(f'Ключ #{key.id}: {error}')
                return dict(kind='customer-moved',user_id=user_id,moved=moved,failed=failed,skipped=skipped,errors=errors)
        if re.fullmatch(r'admin:p:(newproxy|replaceproxy):[0-9]+',payload.action):
            if not payload.confirmed or not payload.request_id:raise HTTPException(409,'Подтвердите выдачу нового ключа')
            from app.services.telemt import TelemtError
            async with database.sessions.begin() as session:
                service=WebProxyService(SQLRepository(session),settings);credential=await session.get(ProxyCredential,int(payload.action.split(':')[3]))
                if not credential or credential.status in ('deleted','migrated'):raise HTTPException(409,'Ключ недоступен')
                customer=await session.get(User,credential.user_id);server=await service.server(credential.server_id)
                if not customer or customer.status=='blocked':raise HTTPException(409,'Клиент недоступен')
                expiry=credential.expires_at.replace(tzinfo=timezone.utc)
                if expiry<=now():raise HTTPException(409,'Срок доступа закончился; сначала продлите доступ')
                kind=await service.repo.setting(f'proxy_credential_kind:{credential.id}') or await service.access_kind(customer)
                if kind not in ('vpn','paid'):raise HTTPException(409,'Не удалось определить тип доступа')
                try:
                    new,link=await service.provision(server,customer,expiry,await service.repo.setting(f'proxy_credential_ip_limit:{credential.id}','2'),None,kind,connection_token=payload.request_id.replace('-',''))
                except (ValueError,TelemtError) as error:raise HTTPException(409,str(error)) from error
                warning=''
                if ':replaceproxy:' in payload.action:
                    try:await service.api(server).disable_user(credential.remote_username)
                    except (ValueError,TelemtError):warning='Новый ключ создан, но старый не удалось отключить. Старый доступ сохранён; повторите его остановку после проверки сервера.'
                    else:credential.status='migrated';credential.current_connections=0;credential.active_unique_ips=0
                await service.repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='web_proxy_replace' if ':replaceproxy:' in payload.action else 'web_proxy_new',target=str(credential.id),details=f'new={new.id}; warning={warning}'))
                return dict(kind='created',credential_id=new.id,warning=warning)
        if not re.fullmatch(r'admin:p:(?:server|users|cred|settings|diagnose|sync|retry|servertoggle|serverdeleteconfirm|serverdelete|move|moveapply|moveall|moveallconfirm|moveallapply|credsync|credtoggle|creddeleteconfirm|creddelete|audience|audienceapply|item|toggle|deleteconfirm|delete)(?::[a-z0-9]+){1,3}|admin:p:input:(?:price|grant|server|serveredit|servername|serverpriority|serverlimits|serverapi|name|connection|add)(?::[0-9]+)?',payload.action):raise HTTPException(422,'Недопустимая операция')
        if any(':'+part+':' in payload.action for part in ('serverdelete','creddelete','delete','moveapply','moveallapply')) and not payload.confirmed:raise HTTPException(409,'Подтвердите операцию')
        async with database.sessions.begin() as session:
            service=WebProxyService(SQLRepository(session),settings)
            try:
                if payload.action.split(':')[3] in ('server','serveredit') and payload.text:
                    existing=await service.server(payload.action.split(':')[4]) if payload.action.split(':')[3]=='serveredit' else None
                    item=await service.save_server(payload.text,existing)
                    screen=await service.render('admin:p:server:'+str(item.id),actor)
                elif payload.text:
                    text=payload.text
                    if payload.action=='admin:p:input:grant':
                        parts=text.split('|');identity=parts[0].strip()
                        u=await session.scalar(select(User).where(or_(User.freeman_id.in_(freeman_id_aliases(identity)),User.email==identity.lower(),User.telegram_id==int(identity) if identity.isdigit() else False)))
                        if not u or not u.telegram_id:raise ValueError('Не найден клиент с Telegram; используйте Freeman ID, Telegram ID или email')
                        parts[0]=str(u.telegram_id);text='|'.join(parts)
                    screen=await service.input(payload.action,actor,text)
                else:screen=await service.render(payload.action,actor)
            except (ValueError,PermissionError) as error:raise HTTPException(409,str(error)) from error
            result=screen_json(screen)
            result['action']=payload.action
            result['title']=next((getattr(b,'text','') for b in getattr(screen.rich,'blocks',[]) if b.__class__.__name__=='RichBlockSectionHeading'),result['text'].split('\n')[0])
            result['note']=' '.join(getattr(b,'text','') for b in getattr(screen.rich,'blocks',[]) if b.__class__.__name__=='RichBlockParagraph')
            buttons=[b for row in result['rows'] for b in row]
            backs=[b['action'] for b in buttons if re.search(r'К серверу|К серверам|К списку|К настройкам|← Назад|← Отмена',b['label'])]
            result['back']=backs[-1] if backs else 'admin:p:home'
            if payload.action=='admin:p:input:price' or re.match(r'admin:p:(serverdelete|delete):',payload.action):result['kind']='home'
            if re.match(r'admin:p:(server|sync|retry|servertoggle|diagnose|serverdeleteconfirm):',payload.action):
                item=await session.get(ProxyServer,int(payload.action.split(':')[3]))
                if item and result['tables']:
                    result['tables'][0]+=[['Группа',{'all':'Все с доступом','vpn':'С VPN','paid':'Платный доступ'}.get(item.audience,item.audience)],['Приоритет',str(item.priority)],['Лимит IP / TCP',str(item.unique_ip_limit)+' / '+str(item.tcp_connection_limit)],['Проверка',item.last_sync.strftime('%d.%m.%Y %H:%M UTC') if item.last_sync else '—']]
                if '❌ Ошибка: ' in result['text']:result['error']=result['text'].split('❌ Ошибка: ',1)[1].split('\n\n')[0]
                if payload.action.startswith('admin:p:diagnose:'):result['diagnostic']=result['text'].split('Исправление read-only file system',1)[-1].strip()
            if payload.action.startswith('admin:p:creddelete:'):
                deleted=await session.get(ProxyCredential,int(payload.action.split(':')[3]));return await clients_screen(session,deleted.server_id)
            if re.match(r'admin:p:(cred|credsync|credtoggle|creddeleteconfirm):',payload.action):
                c=await session.get(ProxyCredential,int(payload.action.split(':')[3]))
                if c:
                    u=await session.get(User,c.user_id);server=await session.get(ProxyServer,c.server_id)
                    result['credential_id']=c.id
                    result['tables'][0]+=[['Выдан',c.created_at.strftime('%d.%m.%Y %H:%M UTC')],['Тип доступа',{'vpn':'С VPN-подпиской','paid':'Отдельный доступ'}.get(await service.repo.setting(f'proxy_credential_kind:{c.id}'),'Не указан')],['Лимит IP',await service.repo.setting(f'proxy_credential_ip_limit:{c.id}','—')]]
                    result['rows'].insert(0,[dict(label='Выдать новый',action=f'admin:p:newproxy:{c.id}'),dict(label='Заменить ключ',action=f'admin:p:replaceproxy:{c.id}')])
                    result['tables']=[result['tables'][0]+[['Имя',u.display_name or u.username or '—' if u else '—'],['Freeman ID',u.freeman_id or '—' if u else '—'],['Сервер',server.name],['Telemt',c.remote_username],['Синхронизация',c.last_sync.strftime('%d.%m.%Y %H:%M UTC') if c.last_sync else '—']]]
            return result
    @router.post('/api/web/admin/search')
    async def search_post(payload:dict,freeman_session:str|None=Cookie(default=None)):
        return await search(q=payload.get('q',''),kind=payload.get('kind','all'),freeman_session=freeman_session)
    @router.get('/api/web/admin/search')
    async def search(q:str='',kind:str='all',freeman_session:str|None=Cookie(default=None)):
        await allowed(freeman_session)
        from app.web.global_search import search as search_records
        async with database.sessions() as session:
            try:return await search_records(session,settings,q,kind)
            except ValueError as error:raise HTTPException(422,str(error)) from error

    return router
