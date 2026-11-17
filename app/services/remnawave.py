"""Remnawave/VLESS provider foundation and owner-only server management."""
import json
import asyncio
from html import escape
from ipaddress import ip_address
from urllib.parse import quote, urlparse

import aiohttp
from cryptography.fernet import Fernet
from sqlalchemy import select, func

from app.database.models import RemnawaveServer, VlessCredential, AdminAudit, now
from app.services.screens import Screen


class RemnawaveApiError(Exception):
    def __init__(self, message, status=0, needs_gateway_key=False, *, error_code=None):
        super().__init__(message)
        self.status = status
        self.error_code = error_code if isinstance(error_code, str) else None
        self.needs_gateway_key = needs_gateway_key


class RemnawaveApiClient:
    def __init__(self, base_url, token, gateway_token=""):
        self.base_url = base_url.rstrip('/') + ('' if base_url.rstrip('/').endswith('/api') else '/api')
        self.token = token.removeprefix('Bearer ').strip()
        self.gateway_token = gateway_token.strip()
        self.reserve_enabled = True

    async def request(self, method, path, body=None, params=None, *, raw=False):
        if self.reserve_enabled and not raw and path=='/users' and method in ('POST','PATCH') and isinstance(body,dict) and 'hwidDeviceLimit' in body:
            from app.services.device_reserve import encode
            current=None
            if method=='PATCH' and 'description' not in body:
                remote=body.get('id',body.get('uuid'))
                from app.services.remnawave_contract import identity
                identity(remote)
                current=(await self.request('GET',f'/users/{remote}',raw=True)).get('response',{})
            body=encode(body,current)
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json'}
        if self.gateway_token: headers['X-Api-Key'] = self.gateway_token
        try:
            from app.services.panel_transport import session
            async with session() as client:
                async with client.request(method, self.base_url + path, json=body, params=params, headers=headers, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = urlparse(response.headers.get("Location", "")).path
                        protected = location == "/r" or location.startswith("/r/")
                        raise RemnawaveApiError("Перед API находится страница входа Caddy. Нужен отдельный ключ Auth Portal (X-Api-Key)." if protected else "API перенаправляет запрос. Проверьте адрес панели и защиту перед API.", response.status, protected)
                    if response.status not in (200, 201, 204):
                        error_code = None
                        if response.status == 404:
                            try:
                                problem = await response.json(content_type=None)
                                if isinstance(problem, dict):
                                    error_code = problem.get('errorCode')
                            except (ValueError, UnicodeError):
                                pass
                        raise RemnawaveApiError(f'HTTP {response.status}: Remnawave отклонила запрос.', response.status, error_code=error_code)
                    from app.services.remnawave_contract import ApiResponse, explicit_result
                    if response.status == 204: return ApiResponse({}, response.status)
                    try:
                        payload = await response.json(content_type=None)
                    except (ValueError, UnicodeError) as error:
                        raise RemnawaveApiError("API вернул пустой ответ или страницу вместо JSON. Проверьте адрес панели и доступ через защиту сервера.", response.status) from error
                    if not isinstance(payload, dict):
                        raise RemnawaveApiError("Некорректный формат ответа Remnawave API.", response.status)
                    explicit_result(payload)
                    if self.reserve_enabled and not raw:
                        from app.services.device_reserve import decode
                        payload=decode(payload)
                    return ApiResponse(payload, response.status)
        except (aiohttp.ClientError, TimeoutError) as error:
            raise RemnawaveApiError('Не удалось связаться с Remnawave API.') from error

    async def health(self): return await self.request('GET', '/system/health')
    async def nodes(self): return await self.request('GET', '/nodes')
    async def stats(self): return await self.request('GET', '/system/stats')
    async def users(self, start=0, size=1): return await self.request('GET', '/users', params={'start': start, 'size': size})
    async def user(self, remote):
        from app.services.remnawave_contract import identity
        identity(remote)
        return await self.request('GET', f'/users/{remote}')
    async def user_by_username(self, username): return await self.request('GET', '/users/by-username/' + quote(username, safe=''))
    async def create_user(self, payload): return await self.request('POST', '/users', payload)
    async def update_user(self, payload):
        from app.services.remnawave_contract import update_identity, updated
        update_identity(payload)
        result = await self.request('PATCH', '/users', payload)
        updated(result, payload)
        return result
    async def enable_user(self, remote):
        from app.services.remnawave_contract import identity, status
        identity(remote)
        if self.reserve_enabled:
            from app.services.device_reserve import has_reserve
            current=(await self.request('GET',f'/users/{remote}',raw=True)).get('response',{})
            if not has_reserve(current) and type(current.get('hwidDeviceLimit')) is int and current['hwidDeviceLimit']>0:
                field,value=identity(remote)
                await self.update_user({field:value,'hwidDeviceLimit':current['hwidDeviceLimit']})
        result = await self.request('POST', f'/users/{remote}/actions/enable')
        status(result, remote, 'ACTIVE')
        return result
    async def disable_user(self, remote):
        from app.services.remnawave_contract import identity, status
        identity(remote)
        result = await self.request('POST', f'/users/{remote}/actions/disable')
        status(result, remote, 'DISABLED')
        return result
    async def delete_user(self, remote):
        from app.services.remnawave_contract import identity, deleted
        identity(remote)
        result = await self.request('DELETE', f'/users/{remote}')
        deleted(result)
        return result
    async def devices(self, remote):
        from app.services.remnawave_contract import identity
        identity(remote)
        return await self.request('GET', f'/hwid/devices/{remote}')
    async def delete_device(self, hwid, user_identity=None):
        from app.services.remnawave_contract import identity, devices_removed, invalid
        field, value = identity(user_identity)
        if not isinstance(hwid, str) or not hwid.strip() or len(hwid)>255: raise invalid()
        result = await self.request('POST', '/hwid/devices/delete', {('userId' if field=='id' else 'userUuid'):value,'hwid':hwid})
        devices_removed(result, user_identity, hwid)
        return result
    async def delete_all_devices(self, remote):
        from app.services.remnawave_contract import identity, devices_removed
        field, value = identity(remote)
        result = await self.request('POST', '/hwid/devices/delete-all', {('userId' if field=='id' else 'userUuid'):value})
        devices_removed(result, remote)
        return result
    async def reset_traffic(self, remote):
        from app.services.remnawave_contract import identity, traffic_reset
        identity(remote)
        result = await self.request('POST', f'/users/{remote}/actions/reset-traffic')
        traffic_reset(result, remote)
        return result
    async def revoke_user(self, remote):
        from app.services.remnawave_contract import identity, revoked
        identity(remote)
        result = await self.request('POST', f'/users/{remote}/actions/revoke', {'revokeOnlyPasswords':False})
        revoked(result, remote)
        return result
    async def squads(self): return await self.request('GET', '/internal-squads')


class RemnawaveService:
    STEPS = (
        'Подключение Remnawave · 1 из 2\nВведите адрес панели: https://panel.example.ru\nНазвание и параметры можно изменить после подключения.',
        'Подключение Remnawave · 2 из 2\nВведите API Token из Remnawave → Settings → API Tokens. Сообщение с токеном будет удалено.',
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

    @staticmethod
    def validate_url(value):
        parsed = urlparse(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Введите адрес панели без логина и параметров, например https://panel.example.ru.')
        if parsed.scheme == 'http':
            try: private = ip_address(parsed.hostname).is_private
            except ValueError: private = False
            if not private:
                raise ValueError('Для публичной панели требуется HTTPS.')
        path = parsed.path.rstrip('/')
        if path not in ('', '/api'):
            raise ValueError('Укажите корень панели или адрес, заканчивающийся на /api.')
        return value.rstrip('/')

    async def server(self, server_id):
        item = await self.session.get(RemnawaveServer, int(server_id))
        if not item:
            raise ValueError('Панель Remnawave не найдена.')
        return item

    def credentials(self, item):
        value = self.decrypt(item.api_token)
        if value.startswith('{'):
            data = json.loads(value)
            return data['bearer'], data.get('gateway', '')
        return value, ''

    def save_credentials(self, bearer, gateway=''):
        return self.encrypt(json.dumps({'bearer': bearer, 'gateway': gateway}) if gateway else bearer)

    def api(self, item):
        bearer, gateway = self.credentials(item)
        return RemnawaveApiClient(item.api_url, bearer, gateway) if gateway else RemnawaveApiClient(item.api_url, bearer)

    async def refresh(self, item, *, request_timeout=None):
        from app.services.remnawave_stats import refresh
        return await refresh(self,item,request_timeout=request_timeout)

    async def render(self, action, user):
        self.admin(user)
        from app.services.remnawave_bot import RemnawaveBot
        extra = await RemnawaveBot(self.repo, self.settings).render(action, user)
        if extra is not None: return extra
        p = action.split(':')[2:]
        page = p[0]
        if page in ('home', 'status'):
            from app.services.server_status import status_screen
            return await status_screen(self, p)
        if page == 'connections':
            items = list(await self.session.scalars(select(RemnawaveServer).order_by(RemnawaveServer.id)))
            rows = [[(item.name, f'admin:r:server:{item.id}')] for item in items]
            return Screen('⚙️ <b>Подключение панелей</b>\n\nТехнические настройки подключения к API. VPN-серверы отображаются в разделе «Статус серверов».', rows+[[('➕ Подключить панель','admin:r:input:server')],[('← Назад','admin:r:home')]], structured=True)
        if page == 'input':
            wizard = {'version': 2, 'values': []}
            await self.repo.set_setting(f'remnawave_wizard:{user.telegram_id}', json.dumps(wizard))
            return Screen(self.STEPS[0], [[('← Отменить', 'admin:r:home')]])
        if page in ('server', 'sync', 'toggle'):
            item = await self.server(p[1])
            if page == 'sync': await self.refresh(item)
            if page == 'toggle': item.status = 'disabled' if item.status == 'active' else 'active'
            issued = await self.session.scalar(select(func.count()).select_from(VlessCredential).where(VlessCredential.server_id == item.id))
            text = (f'{item.country} · <b>{escape(item.name)}</b>\nСтатус выдачи: {item.status}\nAPI: {item.health}\n'
                    f'Пользователей: {item.users_total}/{item.max_users} · онлайн: {item.users_online} · выдано ботом: {issued}\n'
                    f'Internal Squad: <code>{escape(item.internal_squad_uuid or "не выбран")}</code>\n'
                    f'Последняя проверка: {item.last_sync.strftime("%d.%m.%Y %H:%M UTC") if item.last_sync else "—"}')
            if item.last_error: text += '\nОшибка: ' + escape(item.last_error)
            return Screen(text, [[('🔄 Проверить API', f'admin:r:sync:{item.id}')], [('Включить / выключить выдачу', f'admin:r:toggle:{item.id}')], [('🔑 Ключи панели', f'admin:r:keys:{item.id}:0')], [('➕ Выдать VLESS клиенту', f'admin:r:input:grant:{item.id}')], [('👥 Internal Squads', f'admin:r:squads:{item.id}')], [('✏️ Изменить параметры', f'admin:r:edit:{item.id}')], [('← Назад', 'admin:r:home')]])
        raise ValueError('Раздел Remnawave недоступен.')

    async def input(self, action, user, text):
        self.admin(user)
        if action != 'admin:r:input:server':
            from app.services.remnawave_bot import RemnawaveBot
            return await RemnawaveBot(self.repo, self.settings).input(action, user, text)
        text = text.strip(); key = f'remnawave_wizard:{user.telegram_id}'
        try: wizard = json.loads(await self.repo.setting(key, '{}'))
        except json.JSONDecodeError: wizard = {}
        values = wizard.get('values', []) if wizard.get('version') == 2 else []
        if not isinstance(values, list) or len(values) > 1: values = []
        if not values:
            url = self.validate_url(text)
            duplicate = await self.session.scalar(select(RemnawaveServer).where(RemnawaveServer.api_url == url))
            if duplicate: raise ValueError('Эта панель уже подключена. Изменить токен можно в параметрах панели.')
            await self.repo.set_setting(key, json.dumps({'version': 2, 'values': [url]}))
            return Screen(self.STEPS[1], [[('← Отменить', 'admin:r:home')]], keep_input=True)
        if not 20 <= len(text) <= 2000: raise ValueError('API Token должен содержать от 20 до 2000 символов.')
        url = values[0]
        gateway = ''
        bearer = text
        if wizard.get('pending_bearer'):
            bearer = self.decrypt(wizard['pending_bearer'])
            gateway = text
        api = RemnawaveApiClient(url, bearer, gateway) if gateway else RemnawaveApiClient(url, bearer)
        health_result, squads_result = await asyncio.gather(api.health(), api.squads(), return_exceptions=True)
        if isinstance(health_result, RemnawaveApiError) and health_result.needs_gateway_key:
            await self.repo.set_setting(key, json.dumps({'version': 2, 'values': [url], 'pending_bearer': self.encrypt(bearer)}))
            return Screen('🔐 <b>Дополнительная защита Caddy</b>\nПанель требует ещё один ключ доступа.\nОткройте ' + escape(url.removesuffix('/api') + '/r') + ' → API-keys и создайте ключ.\nПришлите сюда ключ Caddy (X-Api-Key), а не UUID и не токен Remnawave. Сообщение будет удалено.\nТокен Remnawave уже сохранён временно в зашифрованном виде.', [[('← Отменить', 'admin:r:home')]], keep_input=True)
        if isinstance(health_result, Exception):
            raise ValueError(f'Не удалось подключиться к Remnawave: {health_result}') from health_result
        duplicate = await self.session.scalar(select(RemnawaveServer).where(RemnawaveServer.api_url == url))
        if duplicate: raise ValueError('Эта панель уже подключена.')
        squad = ''
        squads_failed = False
        try:
            if isinstance(squads_result, Exception): raise squads_result
            result = squads_result
            data = result.get('response', result)
            squads = data.get('internalSquads', data.get('items', [])) if isinstance(data, dict) else data
            if isinstance(squads, list) and len(squads) == 1:
                squad = str(squads[0].get('uuid') or squads[0].get('id') or '')
        except RemnawaveApiError:
            squads_failed = True
        hostname = urlparse(url).hostname
        item = await self.repo.add(RemnawaveServer(name=hostname[:80], country='VLESS', api_url=url,
            api_token=self.save_credentials(bearer, gateway), internal_squad_uuid=squad,
            priority=100, max_users=10000, health='online', last_sync=now()))
        await self.repo.set_setting(key, '')
        await self.repo.add(AdminAudit(actor_telegram_id=user.telegram_id, action='remnawave_server_save', target=str(item.id), details='API connection'))
        screen = await self.render(f'admin:r:server:{item.id}', user)
        screen.text = '✅ <b>Remnawave подключена по API</b>\n\n' + screen.text
        if not squad:
            screen.text += '\n\nДля выдачи выберите Internal Squad кнопкой ниже.'
            if squads_failed: screen.text += ' Токену также нужен доступ к списку Internal Squads.'
        return screen



class RemnawaveWorker:
    def __init__(self, database, settings): self.database, self.settings = database, settings

    async def once(self, refresh_panels=True):
        from app.services.remnawave_worker import once
        await once(self,refresh_panels=refresh_panels)

    async def run(self):
        import asyncio, logging, time
        from app.services.remnawave_worker import SWEEP_SECONDS
        next_stats = 0
        while True:
            try:
                refresh = time.monotonic() >= next_stats
                if refresh:
                    next_stats = time.monotonic() + SWEEP_SECONDS
                await self.once(refresh_panels=refresh)
            except asyncio.CancelledError: raise
            except Exception: logging.getLogger(__name__).exception('Remnawave sync failed')
            await asyncio.sleep(1)
