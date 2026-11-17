import json
import base64
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from ipaddress import ip_address
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from sqlalchemy import select, func

from app.database.models import (
    VpnServer, VpnCredential, Subscription, AccessKey, AdminAudit, now,
)
from app.services.amnezia_api import AmneziaApiClient, AmneziaApiError
from app.services.screens import Screen, time_remaining


PROTOCOLS = {'amneziawg3': 'AmneziaWG 3.1', 'amneziawg': 'AmneziaWG', 'xray': 'Xray'}


from app.utils.formatting import traffic


def nested_number(payload, *names):
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.lower() in names and isinstance(value, (int, float, Decimal)):
                return int(value)
        for value in payload.values():
            result = nested_number(value, *names)
            if result is not None:
                return result
    return None


class VpnService:
    STEPS = (
        'Шаг 1 из 7. Введите название узла, например: Финляндия 1',
        'Шаг 2 из 7. Введите страну вместе с флагом, например: 🇫🇮 Финляндия',
        'Шаг 3 из 7. Введите HTTPS-адрес Amnezia API, например: https://fi1.example.ru',
        'Шаг 4 из 7. Введите протокол: amneziawg3, amneziawg или xray.\n\nРекомендовано: amneziawg3.',
        'Шаг 5 из 7. Введите приоритет от 1 до 999. Чем меньше число, тем выше сервер.',
        'Шаг 6 из 7. Введите максимальное число клиентов на этом сервере, от 1 до 100000.',
        'Шаг 7 из 7. Введите FASTIFY_API_KEY сервера. Сообщение будет удалено после обработки.',
    )

    def __init__(self, repo, settings):
        self.repo, self.settings, self.session = repo, settings, repo.session

    def admin(self, user):
        if not self.settings.is_owner(user.telegram_id):
            raise PermissionError('Admin access denied')

    def cipher(self):
        if not self.settings.encryption_key:
            raise ValueError('ENCRYPTION_KEY не настроен на сервере бота.')
        return Fernet(self.settings.encryption_key.encode())

    def encrypt(self, value): return self.cipher().encrypt(value.encode()).decode()
    def decrypt(self, value): return self.cipher().decrypt(value.encode()).decode()
    def api(self, server): return AmneziaApiClient(server.api_url, self.decrypt(server.api_key))

    @staticmethod
    def validate_url(value):
        parsed = urlparse(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('Адрес должен иметь вид https://vpn.example.ru без логина, пути и параметров.')
        if parsed.scheme == 'http':
            try: private = ip_address(parsed.hostname).is_private
            except ValueError: private = False
            if not private:
                raise ValueError('Для публичного адреса требуется HTTPS. HTTP разрешён только в приватной сети.')
        return value.rstrip('/')

    async def server(self, server_id):
        item = await self.session.get(VpnServer, int(server_id))
        if not item:
            raise ValueError('VPN-сервер не найден.')
        return item

    async def credential(self, subscription_id, user_id=None):
        query = select(VpnCredential).where(VpnCredential.subscription_id == int(subscription_id))
        if user_id is not None:
            query = query.where(VpnCredential.user_id == user_id)
        return await self.session.scalar(query)

    @staticmethod
    def client_rows(payload):
        rows = payload.get('items', payload.get('clients', payload.get('data', payload))) if isinstance(payload, dict) else payload
        if not isinstance(rows, list): return []
        result=[]
        for row in rows:
            if not isinstance(row, dict): continue
            peers=row.get('peers')
            if isinstance(peers,list): result.extend(peer for peer in peers if isinstance(peer,dict))
            else: result.append(row)
        return result

    async def refresh(self, server):
        try:
            api=self.api(server)
            await api.health()
            info=await api.server()
            load=await api.load()
            first=await api.clients(0,100);pages=list(first.get('items') or []) if isinstance(first,dict) else []
            total=int(first.get('total') or len(pages)) if isinstance(first,dict) else len(pages)
            for skip in range(100,total,100):
                page=await api.clients(skip,100)
                if isinstance(page,dict): pages.extend(page.get('items') or [])
            rows=self.client_rows({'items':pages})
            server.health='online';server.last_error='';server.last_sync=now()
            server.current_clients=max(len(rows),int(info.get('totalPeers') or 0) if isinstance(info,dict) else 0);server.online_clients=sum(bool(row.get('online')) for row in rows)
            server.total_received=sum(int((row.get('traffic') or {}).get('received') or 0) for row in rows)
            server.total_sent=sum(int((row.get('traffic') or {}).get('sent') or 0) for row in rows)
            server.cpu_percent=nested_number(load,'cpu','cpupercent','cpu_percent') or 0
            server.memory_percent=nested_number(load,'memorypercent','memory_percent','rampercent','ram_percent') or 0
            server.disk_percent=nested_number(load,'diskpercent','disk_percent') or 0
            by_id={str(row.get('id')):row for row in rows if row.get('id')}
            for credential in await self.session.scalars(select(VpnCredential).where(VpnCredential.server_id==server.id)):
                row=by_id.get(credential.remote_client_id)
                if not row: continue
                traffic_data=row.get('traffic') or {}
                credential.received=int(traffic_data.get('received') or 0);credential.sent=int(traffic_data.get('sent') or 0)
                handshake=row.get('lastHandshake')
                credential.last_handshake=datetime.fromtimestamp(float(handshake),timezone.utc) if handshake else None
                credential.status='active' if row.get('status','active')=='active' else 'disabled';credential.last_sync=now()
            return True
        except (AmneziaApiError, ValueError) as error:
            server.health='offline';server.last_error=str(error)[:255];server.last_sync=now();return False

    async def available_servers(self):
        return list(await self.session.scalars(select(VpnServer).where(
            VpnServer.status=='active', VpnServer.health!='offline',
            VpnServer.current_clients < VpnServer.max_clients,
        ).order_by(VpnServer.priority,VpnServer.id)))

    async def subscription(self, sid, user):
        sub=await self.repo.subscription(user.id,int(sid))
        if not sub or sub.expires_at.replace(tzinfo=timezone.utc)<=now() or await self.repo.setting(f'subscription_removed:{sub.id}')=='1':
            raise ValueError('Подписка недоступна или закончилась.')
        if await self.repo.setting(f'subscription_status:{sub.id}','active')!='active':
            raise ValueError('Подписка заморожена.')
        return sub

    async def provision(self, sub, user, server):
        if server.status!='active' or server.health=='offline' or server.current_clients>=server.max_clients:
            raise ValueError('Этот сервер сейчас недоступен. Выберите другой.')
        existing=await self.credential(sub.id,user.id)
        expiry=int(sub.expires_at.replace(tzinfo=timezone.utc).timestamp())
        client_name=f'freeman_{user.telegram_id}_{sub.id}'[:64]
        if existing and existing.server_id==server.id:
            try:
                await self.api(server).update_client(existing.remote_client_id,existing.protocol,status='active',expiresAt=expiry)
                existing.status='active';existing.expires_at=sub.expires_at;existing.last_sync=now()
                return existing
            except AmneziaApiError as error:
                if error.status!=404: raise ValueError('Не удалось обновить VPN-доступ.') from error
        try:
            response=await self.api(server).create_client(client_name,server.protocol,expiry)
        except AmneziaApiError as error:
            server.health='offline' if error.status==0 else server.health;server.last_error=str(error)[:255]
            raise ValueError('Не удалось выпустить VPN-конфигурацию. Проверьте сервер и API.') from error
        client=response.get('client') if isinstance(response,dict) else None
        if not isinstance(client,dict) or not client.get('id') or not client.get('config'):
            raise ValueError('Amnezia API не вернул ID и конфигурацию клиента.')
        old_server=await self.session.get(VpnServer,existing.server_id) if existing else None
        if existing and old_server:
            try: await self.api(old_server).delete_client(existing.remote_client_id,existing.protocol)
            except AmneziaApiError: pass
        key_id=await self.repo.setting(f'subscription_key:{sub.id}')
        key=await self.session.get(AccessKey,int(key_id)) if key_id else None
        if not key or key.user_id!=user.id or key.status=='deleted':
            key=await self.repo.add(AccessKey(user_id=user.id,name=f'{server.country} · {server.name}',mode=server.protocol,expires_at=sub.expires_at))
            await self.repo.set_setting(f'subscription_key:{sub.id}',str(key.id))
        else:
            key.mode=server.protocol;key.name=f'{server.country} · {server.name}';key.status='active';key.expires_at=sub.expires_at
        await self.repo.set_setting(f'demo_file:{key.id}','')
        if existing:
            existing.server_id=server.id;existing.access_key_id=key.id;existing.remote_client_id=str(client['id']);existing.config=self.encrypt(str(client['config']));existing.protocol=str(client.get('protocol') or server.protocol);existing.status='active';existing.expires_at=sub.expires_at;existing.last_sync=now()
            credential=existing
        else:
            credential=await self.repo.add(VpnCredential(server_id=server.id,user_id=user.id,subscription_id=sub.id,access_key_id=key.id,remote_client_id=str(client['id']),config=self.encrypt(str(client['config'])),protocol=str(client.get('protocol') or server.protocol),expires_at=sub.expires_at,last_sync=now()))
        await self.repo.add(AdminAudit(actor_telegram_id=user.telegram_id or 0,action='vpn_provision',target=str(credential.id),details=f'{server.country}; {server.name}; {credential.protocol}'))
        return credential

    async def rotate(self,sub,user):
        credential=await self.credential(sub.id,user.id)
        if not credential or credential.status=='deleted':
            raise ValueError('Активное устройство не найдено.')
        server=await self.server(credential.server_id)
        try:
            await self.api(server).delete_client(credential.remote_client_id,credential.protocol)
        except AmneziaApiError as error:
            if error.status!=404: raise ValueError('VPN-сервер не подтвердил отзыв старого ключа.') from error
        credential.status='deleted'
        replacement=await self.provision(sub,user,server)
        await self.repo.add(AdminAudit(actor_telegram_id=user.telegram_id or 0,action='vpn_rotate',target=str(replacement.id),details=f'Отозвано устройство; {server.country}; {server.name}'))
        return replacement

    async def render(self, action, user):
        p=action.split(':');admin=p[0]=='admin'
        if admin: self.admin(user);p=p[2:]
        else: p=p[1:]
        page=p[0]
        if admin and page=='home':
            servers=list(await self.session.scalars(select(VpnServer).order_by(VpnServer.priority,VpnServer.id)))
            icons={'online':'🟢','offline':'🔴','unknown':'⚪'}
            rows=[[(f'{icons.get(item.health,"⚪")} {item.country} · {item.name}',f'admin:v:server:{item.id}')] for item in servers]
            text=f'🖥 <b>VPN-серверы</b>\n\nУзлов: {len(servers)} · онлайн: {sum(s.health=="online" for s in servers)}\nКлиентов: {sum(s.current_clients for s in servers)} · онлайн: {sum(s.online_clients for s in servers)}\nТрафик: ↓ {traffic(sum(s.total_received for s in servers))} · ↑ {traffic(sum(s.total_sent for s in servers))}'
            return Screen(text,rows+[[('➕ Подключить VPN-сервер','admin:v:input:server')],[('📖 Как подготовить сервер','admin:guide:vpn')],[('← Назад','admin:more')]])
        if admin and page=='input':
            kind=p[1]
            if kind not in ('server','serveredit'): raise ValueError('Действие недоступно.')
            wizard={'kind':kind,'item':p[2] if kind=='serveredit' and len(p)>2 else '', 'values':[]}
            await self.repo.set_setting(f'vpn_wizard:{user.telegram_id}',json.dumps(wizard))
            return Screen(self.STEPS[0],[[('← Отменить','admin:v:home')]])
        if admin and page in ('server','sync','toggle'):
            item=await self.server(p[1])
            if page=='sync': await self.refresh(item)
            if page=='toggle': item.status='disabled' if item.status=='active' else 'active'
            issued=await self.session.scalar(select(func.count()).select_from(VpnCredential).where(VpnCredential.server_id==item.id))
            text=f'{item.country} · <b>{escape(item.name)}</b>\nПротокол: {PROTOCOLS.get(item.protocol,item.protocol)}\nПриоритет: {item.priority} · лимит: {item.max_clients}\nСтатус: {item.status} · API: {item.health}\nКлиентов: {item.current_clients}/{item.max_clients} · онлайн: {item.online_clients} · выдано ботом: {issued}\nТрафик: ↓ {traffic(item.total_received)} · ↑ {traffic(item.total_sent)}\nCPU: {item.cpu_percent}% · RAM: {item.memory_percent}% · диск: {item.disk_percent}%\nПоследняя проверка: {item.last_sync.strftime("%d.%m.%Y %H:%M UTC") if item.last_sync else "—"}'
            if item.last_error: text+='\nОшибка: '+escape(item.last_error)
            return Screen(text,[[('🔄 Проверить сейчас',f'admin:v:sync:{item.id}')],[('✏️ Изменить подключение',f'admin:v:input:serveredit:{item.id}')],[('Включить / выключить выдачу',f'admin:v:toggle:{item.id}')],[('← Назад','admin:v:home')]])
        if not admin and page in ('locations','connection','qr','download','link'):
            from app.services.vless_provision import connection
            sub=await self.subscription(p[1],user)
            screen=await connection(self.repo,self.settings,sub,user)
            if screen:return screen
        if not admin and page=='locations':
            sub=await self.subscription(p[1],user);servers=await self.available_servers();current=await self.credential(sub.id,user.id)
            rows=[]
            for item in servers:
                marker='✅ ' if current and current.server_id==item.id else ''
                rows.append([(f'{marker}{item.country} · {item.name}',f'v:select:{sub.id}:{item.id}')])
            return Screen('🌍 <b>Выберите VPN-сервер</b>\n\nМожно сменить страну позже. При смене создаётся новая конфигурация, старая отзывается.',rows+[[('← Назад',f'sub:{sub.id}')]])
        if not admin and page=='select':
            sub=await self.subscription(p[1],user);server=await self.server(p[2]);await self.provision(sub,user,server)
            return await self.render(f'v:connection:{sub.id}',user)
        if not admin and page in ('connection','qr','download','link'):
            sub=await self.subscription(p[1],user);credential=await self.credential(sub.id,user.id)
            if not credential:
                return await self.render(f'v:locations:{sub.id}',user)
            server=await self.server(credential.server_id);config=self.decrypt(credential.config)
            rows=[[('📲 Установить подписку',f'install:{sub.id}')],
                  [('🔗 Скопировать ключ','copy:'+config) if len(config)<=256 else ('🔗 Показать ключ',f'v:link:{sub.id}')],
                  [('📄 Скачать конфигурацию',f'v:download:{sub.id}')],
                  [('📖 Подробные инструкции',f'instructions:{sub.id}')],
                  [('🎬 Видеоинструкция',f'video_instruction:{sub.id}')],
                  [('📱 Моё устройство',f'devices:{sub.id}')],
                  [('🔄 Продлить',f'renew:{sub.id}')],
                  [('⚙️ Дополнительно',f'subscription_extra:{sub.id}')],
                  [('🌍 Сменить сервер',f'v:locations:{sub.id}')],
                  [('← Назад',f'sub:{sub.id}')]]
            text=f'🔑 <b>VPN-подключение</b>\n\nСервер: {escape(server.country)} · {escape(server.name)}\nПротокол: {PROTOCOLS.get(credential.protocol,credential.protocol)}\nСтатус: {"Активен" if credential.status=="active" else "Отключён"}\nОсталось: {time_remaining(credential.expires_at)}\nТрафик: ↓ {traffic(credential.received)} · ↑ {traffic(credential.sent)}\n\n<b>Как подключить</b>\n1. Установите AmneziaVPN или AmneziaWG.\n2. Отсканируйте QR-код либо скачайте конфигурацию.\n3. Импортируйте ключ и включите подключение.\n\nОдин ключ предназначен для одного телефона.'
            if page=='download': return Screen(text,rows,document=(config.encode(),f'freeman-{sub.id}-{server.country[:20]}.vpn.txt'))
            if page=='link':
                shown=config if len(config)<=3500 else config[:3500]+'…'
                return Screen(text+'\n\n<code>'+escape(shown)+'</code>'+('\n\nПолный ключ скачайте файлом.' if len(config)>3500 else ''),rows)
            try: qr=await self.api(server).qr(config)
            except AmneziaApiError as error: raise ValueError('Не удалось создать QR-код. Скачайте ключ файлом.') from error
            items=qr.get('items') if isinstance(qr,dict) else None
            if not isinstance(items,list) or not items: raise ValueError('Amnezia API не вернул QR-код. Скачайте ключ файлом.')
            index=int(p[2]) if page=='qr' and len(p)>2 else 0
            if not 0<=index<len(items): raise ValueError('Часть QR-кода недоступна.')
            try: image=base64.b64decode(items[index].split(',',1)[1],validate=True)
            except (ValueError,IndexError): raise ValueError('Amnezia API вернул повреждённый QR-код.')
            if len(items)>1:
                nav=[]
                if index: nav.append(('← QR',f'v:qr:{sub.id}:{index-1}'))
                if index+1<len(items): nav.append(('QR →',f'v:qr:{sub.id}:{index+1}'))
                rows.insert(0,nav);text+=f'\n\nQR-код: часть {index+1} из {len(items)}. Отсканируйте все части по порядку.'
            else: text+='\n\nОтсканируйте QR-код в приложении AmneziaVPN или AmneziaWG.'
            return Screen(text,rows,photo=(image,f'freeman-{sub.id}-qr-{index+1}.png'))
        raise ValueError('Раздел недоступен.')

    async def save_server(self, values, item=None):
        name,country,url,protocol,priority,max_clients,api_key=values
        if not 1<=len(name)<=80 or not 1<=len(country)<=80 or protocol not in PROTOCOLS or not priority.isdigit() or not 1<=int(priority)<=999 or not max_clients.isdigit() or not 1<=int(max_clients)<=100000 or len(api_key)<32 or len(api_key)>1000:
            raise ValueError('Проверьте название, страну, протокол, приоритет, лимит и API-ключ длиной не менее 32 символов.')
        url=self.validate_url(url);api=AmneziaApiClient(url,api_key)
        duplicate=await self.session.scalar(select(VpnServer).where(VpnServer.api_url==url))
        if duplicate and (not item or duplicate.id!=item.id): raise ValueError('Этот URL уже подключён как другой VPN-сервер.')
        try: await api.health();info=await api.server()
        except AmneziaApiError as error: raise ValueError(f'Не удалось подключиться к Amnezia API: {error}') from error
        supported=info.get('protocols') or info.get('enabledProtocols') or [] if isinstance(info,dict) else []
        if supported and protocol not in supported: raise ValueError(f'Сервер не сообщает поддержку {protocol}.')
        if item:
            item.name=name;item.country=country;item.api_url=url;item.api_key=self.encrypt(api_key);item.protocol=protocol;item.priority=int(priority);item.max_clients=int(max_clients);item.health='online';item.last_error='';item.last_sync=now()
        else:
            item=await self.repo.add(VpnServer(name=name,country=country,api_url=url,api_key=self.encrypt(api_key),protocol=protocol,priority=int(priority),max_clients=int(max_clients),health='online',last_sync=now()))
        return item

    async def input(self, action, user, text):
        self.admin(user);p=action.split(':');kind=p[3];text=text.strip();key=f'vpn_wizard:{user.telegram_id}'
        if kind not in ('server','serveredit'): raise ValueError('Действие недоступно.')
        if text.count('|')==6:
            values=[value.strip() for value in text.split('|')]
            item=await self.save_server(values,await self.server(p[4]) if kind=='serveredit' else None)
        else:
            try: wizard=json.loads(await self.repo.setting(key,'{}'))
            except json.JSONDecodeError: wizard={}
            values=wizard.get('values') if wizard.get('kind')==kind else []
            if not isinstance(values,list) or len(values)>=7: values=[]
            step=len(values)
            if step in (0,1) and not 1<=len(text)<=80: raise ValueError('Введите от 1 до 80 символов.')
            if step==2: self.validate_url(text)
            if step==3 and text not in PROTOCOLS: raise ValueError('Введите amneziawg3, amneziawg или xray.')
            if step==4 and (not text.isdigit() or not 1<=int(text)<=999): raise ValueError('Приоритет: целое число от 1 до 999.')
            if step==5 and (not text.isdigit() or not 1<=int(text)<=100000): raise ValueError('Лимит: целое число от 1 до 100000.')
            if step==6 and not 32<=len(text)<=1000: raise ValueError('FASTIFY_API_KEY должен содержать не менее 32 символов.')
            values.append(text)
            if len(values)<7:
                wizard.update(kind=kind,item=wizard.get('item',''),values=values);await self.repo.set_setting(key,json.dumps(wizard))
                return Screen(self.STEPS[len(values)],[[('← Отменить','admin:v:home')]],keep_input=True)
            item_id=wizard.get('item') or (p[4] if kind=='serveredit' and len(p)>4 else '')
            item=await self.save_server(values,await self.server(item_id) if kind=='serveredit' else None)
        await self.repo.set_setting(key,'')
        await self.repo.add(AdminAudit(actor_telegram_id=user.telegram_id or 0,action='vpn_server_save',target=str(item.id),details=f'{item.country}; {item.name}; {item.protocol}'))
        return await self.render(f'admin:v:server:{item.id}',user)

    async def set_subscription_status(self, subscription_id, status):
        from app.database.models import VlessCredential
        c=await self.session.scalar(select(VlessCredential).where(VlessCredential.subscription_id==int(subscription_id)))
        if c:
            from app.services.vless_provision import control
            if status=='active':
                sub=await self.session.get(Subscription,int(subscription_id))
                await control(self.repo,self.settings,c,'extend',sub.expires_at);c.expires_at=sub.expires_at
            await control(self.repo,self.settings,c,{'active':'unblock','deleted':'delete'}.get(status,'block'))
            c.status='active' if status=='active' else 'deleted' if status=='deleted' else 'disabled'
            return
        credential=await self.credential(subscription_id)
        if not credential: return
        server=await self.server(credential.server_id)
        try:
            if status=='deleted': await self.api(server).delete_client(credential.remote_client_id,credential.protocol)
            else: await self.api(server).update_client(credential.remote_client_id,credential.protocol,status='active' if status=='active' else 'disabled',expiresAt=int(credential.expires_at.replace(tzinfo=timezone.utc).timestamp()))
        except AmneziaApiError as error:
            raise ValueError('VPN-сервер не подтвердил изменение доступа. Повторите позже.') from error
        credential.status='deleted' if status=='deleted' else status;credential.last_sync=now()


class VpnWorker:
    def __init__(self,database,settings): self.database,self.settings=database,settings
    async def once(self):
        from app.database.repositories import SQLRepository
        async with self.database.sessions.begin() as session:
            service=VpnService(SQLRepository(session),self.settings)
            for server in await session.scalars(select(VpnServer).where(VpnServer.status=='active').order_by(VpnServer.id)):
                await service.refresh(server)
    async def run(self):
        import asyncio,logging
        while True:
            try: await self.once()
            except asyncio.CancelledError: raise
            except Exception: logging.getLogger(__name__).exception('VPN server sync failed')
            await asyncio.sleep(300)
