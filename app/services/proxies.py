import json, re, secrets, uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from ipaddress import ip_address
from urllib.parse import urlparse, parse_qs, urlencode
from html import escape
from cryptography.fernet import Fernet
from sqlalchemy import select, func, update
from app.database.models import (ProxyEndpoint, ProxyVisit, ProxyServer, ProxyCredential,
    Subscription, User, AppSetting, AdminAudit, Payment, ChatNotice, now)
from app.services.screens import Screen, money
from app.services.telemt import TelemtClient, TelemtError

AUDIENCES={'vpn':'VPN-подписка','paid':'Отдельная покупка','all':'Все с доступом'}

from app.utils.formatting import traffic

def rich_stats(title,pairs,note=''):
    from aiogram.types import RichMessage,RichBlockSectionHeading,RichBlockParagraph,RichBlockTable,RichBlockTableCell
    def cell(value,header=False,align='left'):
        return RichBlockTableCell(align=align,valign='middle',text=str(value),is_header=header)
    blocks=[RichBlockSectionHeading(text=title,size=2),RichBlockTable(cells=[
        [cell('Показатель',True),cell('Значение',True,'right')],
        *[[cell(label),cell(value,False,'right')] for label,value in pairs],
    ],is_bordered=True,is_striped=True,is_compact=True)]
    if note: blocks.append(RichBlockParagraph(text=note))
    return RichMessage(blocks=blocks)


def default_proxy_photo():
    from pathlib import Path
    from aiogram.types import BufferedInputFile

    image=Path(__file__).resolve().parents[1]/'web/static/referral-character.png'
    return BufferedInputFile(image.read_bytes(),filename='telegram-proxy.png')

class ProxyPlacementError(ValueError):
    """A server rejected placement before any provider operation."""


class ProxyService:
    SERVER_STEPS = (
        'Шаг 1 из 7. Введите понятное название сервера, например: Финляндия 1',
        'Шаг 2 из 7. Введите URL API Telemt, например: https://proxy.example.ru:9091',
        'Шаг 3 из 7. Введите группу: vpn, paid или all.\n\nvpn — бесплатно с VPN; paid — отдельная покупка; all — обе группы.',
        'Шаг 4 из 7. Введите приоритет от 1 до 999. Чем меньше число, тем выше сервер в списке.',
        'Шаг 5 из 7. Введите лимит уникальных IP на клиента, от 2 до 100.',
        'Шаг 6 из 7. Введите лимит одновременных TCP-соединений, от 2 до 1000.',
        'Шаг 7 из 7. Введите auth_header из секции [server.api] Telemt. Сообщение будет удалено после обработки.',
    )
    def __init__(self,repo,settings,*,api_timeout=10):
        self.repo,self.settings,self.session=repo,settings,repo.session
        self.api_timeout=api_timeout
    def admin(self,user,action=None):
        if not self.settings.is_owner(user.telegram_id) and not (action=='admin:p:input:grant' and self.settings.role(user.telegram_id)=='manager'): raise PermissionError('Admin access denied')
    def cipher(self):
        if not self.settings.encryption_key: raise ValueError('Ключ шифрования прокси не настроен.')
        return Fernet(self.settings.encryption_key.encode())
    def encrypt(self,value): return self.cipher().encrypt(value.encode()).decode()
    def decrypt(self,value): return self.cipher().decrypt(value.encode()).decode()

    async def access_kind(self,user):
        if user.status=='blocked' or await self.session.scalar(select(User.status).where(User.id==user.id))=='blocked': return ''
        from app.services.proxy_control import config
        cfg=await config(self.repo)
        subs=await self.session.scalars(select(Subscription).where(Subscription.user_id==user.id,Subscription.expires_at>now()))
        for sub in subs:
            if cfg['auto_included'] and (not cfg['eligible_tariffs'] or sub.tariff_id in cfg['eligible_tariffs']) and await self.repo.setting(f'subscription_status:{sub.id}','active')=='active' and await self.repo.setting(f'subscription_removed:{sub.id}')!='1' and await self.repo.setting(f'subscription_transferred:{sub.id}')!='1':
                if not await self.repo.setting(f'proxy_limit:{user.id}'):
                    await self.repo.set_setting(f'proxy_limit:{user.id}','2')
                if not await self.repo.setting(f'proxy_tcp_limit:{user.id}'):
                    await self.repo.set_setting(f'proxy_tcp_limit:{user.id}','2')
                return 'vpn'
        raw=await self.repo.setting(f'proxy_paid_until:{user.id}')
        if raw:
            try:
                if datetime.fromisoformat(raw)>now():
                    # Normalize legacy purchases to the current two-device plan
                    # without overwriting explicit limits set by an administrator.
                    if not await self.repo.setting(f'proxy_limit:{user.id}'):
                        await self.repo.set_setting(f'proxy_limit:{user.id}','2')
                    if not await self.repo.setting(f'proxy_tcp_limit:{user.id}'):
                        await self.repo.set_setting(f'proxy_tcp_limit:{user.id}','2')
                    return 'paid'
            except ValueError: pass
        from app.database.models import ProxyAccessDetails
        if await self.session.scalar(select(ProxyCredential.id).join(ProxyAccessDetails,ProxyAccessDetails.credential_id==ProxyCredential.id).where(ProxyCredential.user_id==user.id,ProxyCredential.status=='active',ProxyCredential.expires_at>now(),ProxyAccessDetails.source.in_(('standalone','admin'))).limit(1)):return 'paid'
        return ''

    async def access_expiry(self,user,kind=None):
        kind=kind or await self.access_kind(user)
        if kind=='vpn':
            subs=list(await self.session.scalars(select(Subscription).where(Subscription.user_id==user.id,Subscription.expires_at>now()).order_by(Subscription.expires_at.desc())))
            for sub in subs:
                if await self.repo.setting(f'subscription_status:{sub.id}','active')=='active': return sub.expires_at.replace(tzinfo=timezone.utc)
        if kind=='paid':
            raw=await self.repo.setting(f'proxy_paid_until:{user.id}')
            current=await self.session.scalar(select(func.max(ProxyCredential.expires_at)).where(ProxyCredential.user_id==user.id,ProxyCredential.status=='active',ProxyCredential.expires_at>now()))
            if current:return current.replace(tzinfo=timezone.utc)
            if raw:return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        return None
    async def eligible(self,user): return bool(await self.access_kind(user))
    async def endpoint(self,id):
        item=await self.session.get(ProxyEndpoint,int(id))
        if not item or item.status=='deleted': raise ValueError('Прокси недоступен.')
        return item
    async def server(self,id):
        item=await self.session.get(ProxyServer,int(id))
        if not item: raise ValueError('Сервер прокси не найден.')
        return item
    def api(self,item): return TelemtClient(item.api_url,self.decrypt(item.auth),timeout=self.api_timeout)
    def legacy_url(self,item): return 'https://t.me/proxy?'+urlencode({'server':item.host,'port':item.port,'secret':self.decrypt(item.secret)})
    def url(self,item): return self.legacy_url(item)
    @staticmethod
    def public_link(link): return 'https://t.me/proxy?'+link.split('?',1)[1] if link.startswith('tg://proxy?') else link
    @staticmethod
    def choose_link(info):
        links=(info or {}).get('links') or {}
        for key in ('tls','secure','classic'):
            values=links.get(key) or []
            if values: return ProxyService.public_link(values[0])
        return ''
    async def matching_servers(self,kind):
        from app.services.proxy_control import select_servers
        return await select_servers(self,kind)

    async def refresh_server(self,item):
        try:
            api=self.api(item);await api.health();users=await api.users();users=users if isinstance(users,list) else []
            item.health='online';item.last_error='';item.last_sync=now();item.remote_users=len(users)
            item.current_connections=sum(int(row.get('current_connections') or 0) for row in users)
            item.active_users=sum(int(row.get('current_connections') or 0)>0 for row in users)
            item.total_octets=sum(int(row.get('total_octets') or 0) for row in users)
            by_name={row.get('username'):row for row in users}
            for credential in await self.session.scalars(select(ProxyCredential).where(
                ProxyCredential.server_id==item.id,
                ProxyCredential.status.not_in(('deleted','migrated')),
            )):
                row=by_name.get(credential.remote_username)
                if row:
                    credential.current_connections=int(row.get('current_connections') or 0);credential.active_unique_ips=int(row.get('active_unique_ips') or 0);credential.total_octets=int(row.get('total_octets') or 0);credential.status='active' if row.get('enabled',True) else 'disabled';credential.last_sync=now()
                    await self.repo.set_setting(f'proxy_stats_seen:{credential.id}','1')
                    link=self.choose_link(row)
                    if link: credential.link=self.encrypt(link)
                elif credential.status not in ('deleted','expired'):
                    credential.status='missing';credential.current_connections=0;credential.active_unique_ips=0;credential.last_sync=now()
            from app.services.proxy_control import observe
            await observe(self.repo,item,users)
            return True
        except (TelemtError,ValueError) as error:
            item.health='offline';item.last_error=str(error)[:255];item.last_sync=now();return False

    async def provision(self,item,user,expiry=None,limit=None,tcp_limit=None,force_kind=None,connection_token=None):
        await self.session.execute(update(ProxyServer).where(ProxyServer.id==item.id).values(status=ProxyServer.status))
        await self.session.refresh(item)
        if not user or user.status=='blocked':raise ValueError('Клиент недоступен')
        from app.services.proxy_control import fresh,server_config
        opts=await server_config(self.repo,item.id)
        if not fresh(item):raise ProxyPlacementError('Сервер не прошёл актуальную проверку')
        username=f'freeman_{user.id}' if not connection_token else f'freeman_{user.id}_{connection_token}'
        exists=await self.session.scalar(select(ProxyCredential.id).where(ProxyCredential.server_id==item.id,ProxyCredential.remote_username==username))
        if not exists:
            count=await self.session.scalar(select(func.count(ProxyCredential.id)).where(ProxyCredential.server_id==item.id,ProxyCredential.status.not_in(('deleted','migrated','missing'))))
            clients=await self.session.scalar(select(func.count(func.distinct(ProxyCredential.user_id))).where(ProxyCredential.server_id==item.id,ProxyCredential.status=='active',ProxyCredential.expires_at>now()))
            if count>=opts['max_accounts'] or clients>=opts['max_clients'] or (opts.get('traffic_limit') and item.total_octets>=opts['traffic_limit']):raise ProxyPlacementError('Сервер достиг заданной ёмкости')
        kind=force_kind or await self.access_kind(user)
        if not kind or item.status!='active' or item.audience not in ('all',kind): raise ProxyPlacementError('Для этого сервера доступ недоступен.')
        expiry=expiry or await self.access_expiry(user,kind)
        if not expiry or expiry<=now(): raise ValueError('Срок доступа закончился.')
        limit=max(1,min(100,int(limit or await self.repo.setting(f'proxy_limit:{user.id}',str(item.unique_ip_limit)))))
        tcp_limit=max(1,min(1000,int(tcp_limit or await self.repo.setting(f'proxy_tcp_limit:{user.id}',str(item.tcp_connection_limit)))))
        body={'max_unique_ips':limit,'max_tcp_conns':tcp_limit,'expiration_rfc3339':expiry.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),'enabled':True};api=self.api(item)
        credential=await self.session.scalar(select(ProxyCredential).where(ProxyCredential.server_id==item.id,ProxyCredential.user_id==user.id,ProxyCredential.remote_username==username))
        try:
            if credential: info=await api.patch_user(username,**body)
            else:
                created=await api.create_user(username,**body);info=created.get('user',created) if isinstance(created,dict) else {}
        except TelemtError as error:
            if error.status not in (409,422):
                await self.repo.set_setting(f'proxy_issue_error:{item.id}',str(error)[:1000])
                raise ValueError('Telemt доступен, но не смог записать пользователя в конфигурацию. Откройте диагностику сервера в админке.') from error
            info=await api.patch_user(username,**body)
        link=self.choose_link(info)
        if not link: info=await api.user(username);link=self.choose_link(info)
        if not link: raise ValueError('Telemt создал доступ, но не вернул ссылку. Проверьте public_host/public_port и активный режим прокси.')
        if credential: credential.link=self.encrypt(link);credential.expires_at=expiry;credential.status='active';credential.last_sync=now()
        else: credential=await self.repo.add(ProxyCredential(server_id=item.id,user_id=user.id,remote_username=username,link=self.encrypt(link),expires_at=expiry,last_sync=now()))
        # These values describe what Telemt accepted for this exact credential.
        # Keep them separately from the user's current entitlement, which may change.
        await self.repo.set_setting(f'proxy_credential_kind:{credential.id}',kind)
        await self.repo.set_setting(f'proxy_credential_ip_limit:{credential.id}',str(limit))
        await self.repo.set_setting(f'proxy_issue_error:{item.id}','')
        item.health='online';item.last_error='';item.last_sync=now()
        from app.services.proxy_control import meta
        m=await meta(self.repo,credential);m.ip_limit=limit
        if kind=='vpn':m.source='included'
        return credential,link

    async def provision_or_reuse(self,item,user,kind):
        """Return a live personal link without rewriting Telemt on every view."""
        expiry=await self.access_expiry(user,kind)
        credential=await self.session.scalar(select(ProxyCredential).where(
            ProxyCredential.server_id==item.id,ProxyCredential.user_id==user.id,ProxyCredential.remote_username.in_((f'freeman_{user.telegram_id}',f'freeman_{user.id}')),
        ))
        if credential and credential.status=='active' and credential.link and expiry:
            stored=credential.expires_at.replace(tzinfo=timezone.utc)
            # Re-provision only when access was extended or the record is stale.
            if stored>=expiry-timedelta(seconds=30) and stored>now():
                try: return credential,self.public_link(self.decrypt(credential.link))
                except (ValueError,TypeError): pass
        return await self.provision(item,user,expiry=expiry,force_kind=kind)

    async def customer_screen(self,user,offset=0):
        from app.services.proxy_control import plans,config,quote_proxy,meta,sync_included
        cfg=await config(self.repo)
        if user.status=='blocked':raise ValueError('Аккаунт заблокирован')
        kind=await self.access_kind(user)
        keys=list(await self.session.scalars(select(ProxyCredential).where(ProxyCredential.user_id==user.id,ProxyCredential.status.not_in(('deleted','migrated'))).order_by(ProxyCredential.id.desc())))
        from app.database.models import ProxyAccessDetails
        included=set(await self.session.scalars(select(ProxyAccessDetails.credential_id).where(ProxyAccessDetails.credential_id.in_([k.id for k in keys]),ProxyAccessDetails.source=='included'))) if kind=='vpn' and keys else set()
        if kind=='vpn' and not any(k.id in included and k.status=='active' and k.expires_at.replace(tzinfo=timezone.utc)>now() for k in keys):
            # Provisioning is handled by the existing worker, not a navigation transaction.
            from app.services.proxy_control import queue_included,subscription_eligible
            for sub in await self.session.scalars(select(Subscription).where(Subscription.user_id==user.id,Subscription.expires_at>now()).order_by(Subscription.expires_at.desc())):
                if await subscription_eligible(self.repo,sub):
                    await queue_included(self.repo,sub);break
        lines=['🌐 <b>Telegram Proxy</b>',
               '💬 Пользуйтесь Telegram без включённого VPN.',
               '<blockquote>🎁 <b>Бесплатно вместе с VPN</b>\n\n'
               'Клиентам с активной VPN-подпиской выдаём один персональный прокси на аккаунт '
               'на срок VPN. Купили VPN на месяц — прокси тоже доступен на месяц. '
               'При продлении VPN продлевается и бесплатный доступ к прокси.</blockquote>',
               '🛒 <b>Можно купить отдельно</b>\n\nБез VPN-подписки тоже можно пользоваться Telegram Proxy. '
               'Стоимость и срок отдельного доступа указаны ниже.',
               '<blockquote>📱 <b>Один прокси — два устройства</b>\n\n'
               'Можно подключить, например, два телефона. Не передавайте персональную ссылку: '
               'при превышении лимита двух активных устройств новые подключения не будут работать.</blockquote>']
        rows=[[('🛒 Купить прокси','p:purchase')]] if cfg['standalone_enabled'] else []
        options={'p:purchase':{'style':'success'}}
        keys=[c for c in keys if await self.repo.setting(f'proxy_included_duplicate:{c.id}')!='1']
        total=len(keys);offset=min(max(0,offset),max(0,(total-1)//5*5))
        for number,c in enumerate(keys[offset:offset+5],offset+1):
            m=await meta(self.repo,c);live=c.status=='active' and c.expires_at.replace(tzinfo=timezone.utc)>now()
            source={'included':'Включён в подписку','standalone':'Куплен отдельно','admin':'Выдан администратором'}.get(m.source,'Источник не определён')
            rows.append([(f'🌐 Прокси №{number} · '+('🎁 VPN' if m.source=='included' else '✅ Активен' if live else '⌛ Неактивен'),f'p:access:{c.id}')])
            if not live:options[f'p:access:{c.id}']={'style':'success'}
        if not keys:lines.append('\nПодключений пока нет.')
        if cfg['standalone_enabled']:
            active=next((p for p in await plans(self.repo) if p.enabled),None)
            if active:
                price,_=await quote_proxy(self.repo,user.id,active)
                from app.services.bot_catalog import price_text
                lines.append(f'💳 <b>Отдельный доступ</b>\n\n{active.days} дней · '+price_text(active.price,price,html=True)+f' · до {active.ip_limit} устройств / уникальных IP.')
                pass
        if offset:rows.append([('← Предыдущие',f'p:home:{max(0,offset-5)}')])
        if offset+5<total:rows.append([('Следующие →',f'p:home:{offset+5}')])
        rows.append([('← Назад','home')])
        return Screen('\n\n'.join(lines),rows,button_options=options,structured=True,photo=default_proxy_photo())

    async def migration_targets(self,source,customer=None):
        kind=await self.access_kind(customer) if customer else ''
        query=select(ProxyServer).where(
            ProxyServer.id!=source.id,
            ProxyServer.status=='active',
        ).order_by(ProxyServer.priority,ProxyServer.id)
        targets=list(await self.session.scalars(query))
        return [item for item in targets if not kind or item.audience in ('all',kind)]

    async def move_credential(self,credential,target,actor):
        source=await self.server(credential.server_id)
        if source.id==target.id or target.status!='active':
            raise ValueError('Выберите другой сервер с включённой выдачей.')
        customer=await self.session.get(User,credential.user_id)
        if not customer or customer.status=='blocked':
            raise ValueError('Клиент не найден или заблокирован.')
        kind=await self.access_kind(customer)
        if not kind or target.audience not in ('all',kind):
            raise ValueError('Группа нового сервера не подходит доступу клиента.')
        expiry=credential.expires_at.replace(tzinfo=timezone.utc)
        if expiry<=now():
            raise ValueError('Срок этого ключа уже закончился.')
        new_credential,link=await self.provision(
            target,customer,expiry,
            await self.repo.setting(f'proxy_limit:{customer.id}','2'),
            await self.repo.setting(f'proxy_tcp_limit:{customer.id}','2'),kind,
        )
        revoked=True;warning=''
        try:
            await self.api(source).disable_user(credential.remote_username)
        except (TelemtError,ValueError) as error:
            # Failover must still work while the old server is unavailable. The
            # audit record makes the unconfirmed revoke visible to the owner.
            revoked=False;warning=str(error)[:500]
        if revoked:credential.status='migrated';credential.current_connections=0;credential.active_unique_ips=0;credential.last_sync=now()
        await self.repo.add(AdminAudit(
            actor_telegram_id=actor.telegram_id,action='proxy_key_move',target=str(credential.id),
            details=f'{source.id}->{target.id}; new={new_credential.id}; revoked={revoked}; {warning}',
        ))
        return new_credential,link,revoked

    async def render(self,action,user):
        p=action.split(':');admin=p[0]=='admin'
        if admin: self.admin(user,action);p=p[2:]
        else: p=p[1:]
        page=p[0];back=[('← Назад','admin:home' if admin and self.settings.role(user.telegram_id)=='manager' else 'admin:p:home' if admin else 'p:home')]
        from app.services.public_proxy import admin_screen, connect_screen, connection_url
        public_url = await connection_url(self.repo)
        if public_url:
            if admin and (page == 'home' or p[:2] in (['input','grant'], ['input','price'])):
                return admin_screen(self.settings, public_url) if self.settings.is_owner(user.telegram_id) else connect_screen(public_url)
            if not admin:
                if page in ('cardpay','cardcheck') and len(p) == 2 and p[1].isdigit():
                    payment = await self.repo.session.get(Payment, int(p[1]))
                    if (payment and payment.user_id == user.id
                            and payment.status.startswith('proxy_order_') and payment.provider_id):
                        screen = await ProxyCheckoutService(self.repo,self.settings).render(f'p:cardcheck:{payment.id}',user)
                        if payment.status == 'proxy_order_paid':
                            return screen
                return admin_screen(self.settings, public_url) if page == 'home' and self.settings.is_owner(user.telegram_id) else connect_screen(public_url)
        if not admin and page=='home':
            if self.settings.is_owner(user.telegram_id):
                from app.services.bot_proxy_grant import BotProxyGrant
                return await BotProxyGrant(self.repo,self.settings).home(user)
            return await self.customer_screen(user,max(0,int(p[1])) if len(p)>1 else 0)
        if not admin and page=='purchase':
            return Screen('Купить дополнительный Telegram Proxy\n\nОдин прокси — два активных устройства / подключения. Не передавайте ссылку другим людям. Выберите способ оплаты.',[[('💰 С баланса','p:buy')],[('💳 Карта / СБП','p:card')],[('← Назад','p:home')]])
        if not admin and page=='access':
            from app.services.proxy_control import meta
            c=await self.session.get(ProxyCredential,int(p[1]))
            if not c or c.user_id!=user.id or c.status in ('deleted','migrated'):raise ValueError('Прокси не найден')
            m=await meta(self.repo,c);live=c.status=='active' and c.expires_at.replace(tzinfo=timezone.utc)>now()
            source={'included':'🎁 Бесплатно вместе с VPN — на срок активной VPN-подписки.',
                    'standalone':'🛒 Отдельная подписка Telegram Proxy.',
                    'admin':'🛠 Доступ выдан администратором.'}.get(m.source,'🌐 Персональный Telegram Proxy.')
            seen=await self.repo.setting(f'proxy_stats_seen:{c.id}')=='1'
            stats=(f'📱 Активных подключений (уникальных IP): {c.active_unique_ips} из {m.ip_limit}\n'
                   f'Одновременных соединений: {c.current_connections}' if seen else
                   '📱 Данные об активных подключениях пока не получены от сервера.')
            if seen and c.last_sync:stats+=f'\nОбновлено: {c.last_sync:%d.%m.%Y %H:%M} UTC'
            text=(f'🌐 <b>Ваш Telegram Proxy</b>\n\n{source}\n\n'
                  f'{"✅ Активен" if live else "⌛ Срок закончился или доступ отключён"}\n\n'
                  f'📅 Действует до: {c.expires_at:%d.%m.%Y}\n\n'
                  f'{stats}\n\n'
                  '<blockquote>💬 Telegram работает без включённого VPN.\n\n'
                  f'Лимит этого доступа: до {m.ip_limit} активных устройств / уникальных IP. '
                  'Не передавайте ссылку другим людям: при превышении лимита новые подключения не работают.</blockquote>')
            rows=[];options={'p:purchase':{'style':'success'}}
            if live and c.link:
                target='url:'+self.public_link(self.decrypt(c.link))
                rows.append([('✈️ Подключить прокси',target)]);options[target]={'style':'primary'}
            else:rows.append([('🛒 Купить прокси','p:purchase')])
            rows+=[[('🔄 Обновить',f'p:access:{c.id}')],[('← Назад','p:home')]]
            return Screen(text,rows,structured=True,button_options=options)
        if page=='home':
            servers=list(await self.session.scalars(select(ProxyServer).where(ProxyServer.status!='deleted').order_by(ProxyServer.priority,ProxyServer.id)))
            legacy=list(await self.session.scalars(select(ProxyEndpoint).where(ProxyEndpoint.status!='deleted' if admin else ProxyEndpoint.status=='active').order_by(ProxyEndpoint.id)))
            if admin:
                price=int(await self.repo.setting('proxy_monthly_price','3000'));icons={'online':'🟢','offline':'🔴','unknown':'⚪'}
                rows=[[(f'{icons.get(item.health,"⚪")} {item.name} · {AUDIENCES.get(item.audience,item.audience)}',f'admin:p:server:{item.id}')] for item in servers]
                rows += [[(f'🔗 Старая ссылка: {item.name}',f'admin:p:item:{item.id}')] for item in legacy]
                wizard_steps=0
                try:
                    wizard=json.loads(await self.repo.setting(f'proxy_wizard:{user.telegram_id}','{}'))
                    wizard_steps=len(wizard.get('values',[])) if isinstance(wizard.get('values'),list) else 0
                except json.JSONDecodeError:
                    pass
                text=f'🌐 <b>Прокси-серверы Telemt</b>\n\nСерверов: {len(servers)} · онлайн: {sum(s.health=="online" for s in servers)}\nАктивных соединений: {sum(s.current_connections for s in servers)}\nТрафик: {traffic(sum(s.total_octets for s in servers))}\nБез VPN: {money(price)} за 30 дней · с VPN бесплатно.\n\nКаждому клиенту выпускается отдельный secret. Несколько серверов одной группы отображаются как основной и резервные.'
                if not servers and wizard_steps==0:
                    text+='\n\n⚠️ Подключённых серверов пока нет. Нажатие кнопки запускает мастер; сервер появится здесь только после всех 7 шагов и успешной проверки API.'
                elif wizard_steps:
                    text+=f'\n\n🟡 Подключение не завершено: заполнено шагов {wizard_steps} из 7. Запустите мастер заново или продолжите ввод в активном диалоге.'
                rows += [[('➕ Подключить сервер Telemt','admin:p:input:server')],[('🎁 Выдать прокси бесплатно','admin:p:input:grant')],[('💰 Цена без VPN','admin:p:input:price')],[('📖 Что настроить на сервере','admin:guide:proxy')],[('← Telegram Proxy','p:home')]]
                options={f'admin:p:server:{item.id}':{'style':'success' if item.status=='active' and item.health=='online' else 'danger' if item.health=='offline' else 'primary'} for item in servers}
                pairs=[('Серверы',len(servers)),('API онлайн',sum(s.health=='online' for s in servers)),('Выдача включена',sum(s.status=='active' for s in servers)),('Активные клиенты',sum(s.active_users for s in servers)),('Соединения',sum(s.current_connections for s in servers)),('Трафик',traffic(sum(s.total_octets for s in servers))),('Цена без VPN',money(price))]
                return Screen(text,rows,button_options=options,rich=rich_stats('Telegram-прокси · управление',pairs,'Зелёный сервер принимает выдачу; красный требует проверки.'))
            kind=await self.access_kind(user)
            if not kind:
                price=int(await self.repo.setting('proxy_monthly_price','3000'));return Screen(f'🌐 <b>Telegram-прокси</b>\n\n🎁 <b>С VPN-подпиской:</b> бесплатно\n💳 <b>Без подписки:</b> {money(price)} за 30 дней\n📅 <b>Примерно в день:</b> {money(round(price/30))}\n\n<b>Лимиты персонального доступа</b>\n📱 Устройства: до 2\n🌐 Уникальные IP: до 2\n⚡ Одновременные подключения: до 2\n\nПередача ссылки другим людям может привести к отклонению лишних подключений.\n\n💰 <b>Баланс:</b> {money(await self.repo.balance(user.id))}',[[('💳 Оплатить картой / СБП','p:card')],[('💰 Оплатить с баланса','p:buy')],[('🛡 Купить VPN-подписку','buy')],[('← Назад','home')]])
            matched=[item for item in servers if item.status=='active' and item.audience in ('all',kind) and item.health!='offline'];rows=[];errors=[]
            for index,item in enumerate(matched):
                try:
                    _,link=await self.provision_or_reuse(item,user,kind)
                    rows.append([(f'🔌 {"Основной" if index==0 else "Резервный"} · {item.name}','url:'+link)])
                except ValueError as error:
                    errors.append(f'{item.name}: {error}')
            rows += [[(f'Старая ссылка · {item.name}',f'p:open:{item.id}')] for item in legacy]
            raw=await self.repo.setting(f'proxy_paid_until:{user.id}');text='Доступ включён вашей VPN-подпиской.' if kind=='vpn' else f'Оплачено до {datetime.fromisoformat(raw):%d.%m.%Y %H:%M} UTC.'
            if rows:
                text+='\n\n✅ Персональная ссылка создана автоматически. Нажмите кнопку подключения ниже.\n\n<b>Ваши лимиты</b>\n📱 Устройства: 2\n🌐 Уникальные IP: 2\n⚡ Одновременные подключения: 2\n\nЕсли основной сервер временно недоступен, используйте резервный.'
            elif matched and errors:
                text+='\n\n⚠️ Сервер подключён, но персональную ссылку сейчас выпустить не удалось. Администратор уже видит ошибку в диагностике.'
            else:
                text+='\n\n⚠️ Доступ активен, но нет включённого Telemt-сервера для вашей группы. Срок доступа сохранён.'
            return Screen('🌐 <b>Персональный Telegram-прокси</b>\n\n'+text,rows+[[('← Назад','home')]])
        if not admin and page in ('card','cardpay','cardcheck'):
            return await ProxyCheckoutService(self.repo,self.settings).render(action,user)
        if not admin and page=='connect':
            item=await self.server(p[1]);credential,link=await self.provision(item,user);limit=await self.repo.setting(f'proxy_limit:{user.id}',str(item.unique_ip_limit));tcp=await self.repo.setting(f'proxy_tcp_limit:{user.id}',str(item.tcp_connection_limit))
            return Screen(f'🌐 <b>{escape(item.name)}</b>\n\n✅ <b>Статус:</b> доступ активен\n📅 <b>Действует до:</b> {credential.expires_at:%d.%m.%Y %H:%M} UTC\n\n<b>Лимиты</b>\n📱 Устройства / уникальные IP: {limit}\n⚡ Одновременные подключения: {tcp}\n\nНе передавайте персональную ссылку другим людям. Если сервер временно недоступен, вернитесь к списку и выберите резервный.',[[('🔌 Подключить в Telegram','url:'+link)],back])
        if not admin and page=='buy':
            from app.services.proxy_control import plans,quote_proxy,config
            if not (await config(self.repo))['standalone_enabled']:raise ValueError('Отдельная покупка отключена')
            plan=next((p for p in await plans(self.repo) if p.enabled),None)
            if not plan:raise ValueError('Тарифы Proxy недоступны')
            price,_=await quote_proxy(self.repo,user.id,plan);token=secrets.token_hex(12)
            await self.repo.set_setting('proxy_purchase:'+token,json.dumps(dict(user=user.id,plan_id=plan.id,price=price,expires=now().timestamp()+300)))
            from app.services.bot_catalog import price_text
            return Screen(f'Подтвердите покупку Telegram Proxy.\n\n{plan.days} дней · до {plan.ip_limit} IP\nК списанию: {price_text(plan.price,price,html=True)}\nБаланс: {money(await self.repo.balance(user.id))}',[[('Подтвердить оплату',f'p:buyapply:{token}')],[('Назад','p:home')]])
        if not admin and page=='buyapply':
            from app.web.proxy_control import Purchase,purchase
            from app.services.proxy_control import plans,quote_proxy
            token=p[1];data=json.loads(await self.repo.setting('proxy_purchase:'+token,'{}'))
            if data.get('user')!=user.id or data.get('expires',0)<now().timestamp():raise ValueError('Подтверждение устарело')
            plan=await self.session.get(__import__('app.database.models',fromlist=['ProxyPlan']).ProxyPlan,data.get('plan_id')) if data.get('plan_id') else next((p for p in await plans(self.repo) if p.enabled),None)
            if not plan or not plan.enabled or (await quote_proxy(self.repo,user.id,plan))[0]!=data['price']:raise ValueError('Стоимость изменилась; выберите тариф заново')
            result=await purchase(self.repo,self.settings,user,Purchase(method='balance',plan_id=plan.id,request_id='bot-'+token))
            return Screen('✅ Оплата принята. Ваш Telegram Proxy готов.',[[('Мои подключения','p:home')]])
        if admin and page=='settings':
            item=await self.server(p[1])
            return Screen(f'⚙️ <b>Настройки сервера</b>\n\n📝 Название: {escape(item.name)}\n👥 Группа выдачи: {AUDIENCES.get(item.audience,item.audience)}\n🔢 Приоритет: {item.priority}\n🌐 Лимит уникальных IP: {item.unique_ip_limit}\n⚡ Лимит TCP-соединений: {item.tcp_connection_limit}\n🔗 API: {escape(item.api_url)}',[[('✏️ Изменить название',f'admin:p:input:servername:{item.id}')],[('👥 Изменить группу выдачи',f'admin:p:audience:{item.id}')],[('🔢 Изменить приоритет',f'admin:p:input:serverpriority:{item.id}')],[('📱 Изменить лимиты',f'admin:p:input:serverlimits:{item.id}')],[('🔐 Изменить API и Authorization',f'admin:p:input:serverapi:{item.id}')],[('← К серверу',f'admin:p:server:{item.id}')]])
        if admin and page in ('audience','audienceapply'):
            item=await self.server(p[1])
            if page=='audienceapply':
                audience=p[2]
                if audience not in AUDIENCES: raise ValueError('Группа сервера недоступна.')
                old=item.audience;item.audience=audience
                issued=await self.provision_existing(item)
                await self.repo.add(AdminAudit(actor_telegram_id=user.telegram_id,action='proxy_server_audience',target=str(item.id),details=f'{old}->{audience}; issued={issued}'))
                return await self.render(f'admin:p:settings:{item.id}',user)
            rows=[]
            for value,label in AUDIENCES.items():
                marker='✅ ' if item.audience==value else ''
                rows.append([(marker+label,f'admin:p:audienceapply:{item.id}:{value}')])
            return Screen(f'👥 <b>Группа выдачи · {escape(item.name)}</b>\n\n<b>VPN-подписка</b> — бесплатно только клиентам с активной VPN-подпиской.\n\n<b>Отдельная покупка</b> — только клиентам, купившим Telegram-прокси отдельно.\n\n<b>Все с доступом</b> — обе группы клиентов.',rows+[[('← Назад',f'admin:p:settings:{item.id}')]])
        if admin and page in ('server','sync','retry','servertoggle','diagnose','serverdeleteconfirm','serverdelete'):
            item=await self.server(p[1])
            if page=='sync': await self.refresh_server(item)
            if page=='retry':
                await self.provision_existing(item)
                await self.refresh_server(item)
            if page=='servertoggle':
                item.status='disabled' if item.status=='active' else 'active'
                await self.repo.add(AdminAudit(actor_telegram_id=user.telegram_id,action='proxy_server_toggle',target=str(item.id),details=item.status))
            if page=='serverdelete':
                item.status='deleted'
                await self.repo.add(AdminAudit(actor_telegram_id=user.telegram_id,action='proxy_server_delete',target=str(item.id),details=item.name))
                return await self.render('admin:p:home',user)
            issue_error=await self.repo.setting(f'proxy_issue_error:{item.id}')
            total=await self.session.scalar(select(func.count()).select_from(ProxyCredential).where(ProxyCredential.server_id==item.id,ProxyCredential.status!='deleted'))
            active=await self.session.scalar(select(func.count()).select_from(ProxyCredential).where(ProxyCredential.server_id==item.id,ProxyCredential.status=='active',ProxyCredential.expires_at>now()))
            disabled=await self.session.scalar(select(func.count()).select_from(ProxyCredential).where(ProxyCredential.server_id==item.id,ProxyCredential.status=='disabled'))
            expired=await self.session.scalar(select(func.count()).select_from(ProxyCredential).where(ProxyCredential.server_id==item.id,ProxyCredential.expires_at<=now()))
            issue_icon='🟢' if item.status=='active' else '🔴';api_icon={'online':'🟢','offline':'🔴'}.get(item.health,'🟡');write_icon='🔴' if issue_error else '🟢'
            pairs=[('Выдача',f'{issue_icon} {"включена" if item.status=="active" else "выключена"}'),('Telemt API',f'{api_icon} {item.health}'),('Запись конфигурации',f'{write_icon} {"ошибка" if issue_error else "без известных ошибок"}'),('Выдано ботом',total),('Активные ключи',active),('Заблокированы',disabled),('Истекли',expired),('Пользователей Telemt',item.remote_users),('Онлайн пользователей',item.active_users),('Соединения',item.current_connections),('Трафик',traffic(item.total_octets))]
            text=f'🌐 <b>{escape(item.name)}</b>\n\n<b>Настройки выдачи</b>\n👥 Группа: {AUDIENCES.get(item.audience,item.audience)}\n🔢 Приоритет: {item.priority}\n📱 Уникальных IP: {item.unique_ip_limit}\n⚡ TCP-соединений: {item.tcp_connection_limit}\n\n<b>Состояние</b>\n{issue_icon} Выдача: {"включена" if item.status=="active" else "выключена"}\n{api_icon} Telemt API: {item.health}\n{write_icon} Запись пользователей: {"ошибка" if issue_error else "готова"}\n\n<b>Ключи клиентов</b>\n🔑 Выдано ботом: {total}\n🟢 Активно: {active}\n🔴 Заблокировано: {disabled}\n⌛ Истекло: {expired}\n\n<b>Нагрузка Telemt</b>\n👥 Пользователей: {item.remote_users}\n🟢 Сейчас онлайн: {item.active_users}\n⚡ Соединений: {item.current_connections}\n📊 Трафик: {traffic(item.total_octets)}\n🕒 Последняя проверка: {item.last_sync.strftime("%d.%m.%Y %H:%M UTC") if item.last_sync else "—"}'
            error=item.last_error or issue_error
            if error: text+='\n\n❌ Ошибка: '+escape(error)
            if page=='diagnose':
                text+='\n\n<b>Исправление read-only file system</b>\nTelemt сохраняет пользователей атомарно: создаёт временный файл рядом с telemt.toml и переименовывает его. В Docker нужно примонтировать весь каталог конфигурации с правом записи, например <code>./telemt-config:/etc/telemt</code>, без <code>:ro</code>. Сам корень контейнера может оставаться read-only. После исправления перезапустите Telemt и нажмите «Повторить выдачу». '
            rows=[[('👥 Ключи и клиенты',f'admin:p:users:{item.id}:0')],[('🔀 Перенести клиентов',f'admin:p:moveall:{item.id}')],[('🔄 Синхронизировать',f'admin:p:sync:{item.id}'),('🩺 Диагностика',f'admin:p:diagnose:{item.id}')]]
            if issue_error: rows.append([('♻️ Повторить выдачу клиентам',f'admin:p:retry:{item.id}')])
            rows += [[('⚙️ Настройки сервера',f'admin:p:settings:{item.id}')],[((('🔴 Выключить выдачу') if item.status=='active' else '🟢 Включить выдачу'),f'admin:p:servertoggle:{item.id}')]]
            if page=='serverdeleteconfirm': rows += [[('❌ Удалить сервер из бота',f'admin:p:serverdelete:{item.id}')]]
            else: rows += [[('🗑 Удалить сервер',f'admin:p:serverdeleteconfirm:{item.id}')]]
            rows += [back]
            options={f'admin:p:servertoggle:{item.id}':{'style':'danger' if item.status=='active' else 'success'},f'admin:p:serverdelete:{item.id}':{'style':'danger'},f'admin:p:sync:{item.id}':{'style':'primary'},f'admin:p:retry:{item.id}':{'style':'success'}}
            note='API отвечает, но запись конфигурации не работает.' if issue_error else 'Статистика обновляется автоматически каждые 5 минут.'
            return Screen(text,rows,button_options=options,rich=rich_stats(item.name,pairs,note))
        if admin and page=='users':
            item=await self.server(p[1]);offset=max(0,int(p[2]))
            query=select(ProxyCredential).where(ProxyCredential.server_id==item.id,ProxyCredential.status!='deleted').order_by(ProxyCredential.id.desc()).offset(offset).limit(6)
            credentials=list(await self.session.scalars(query));rows=[];lines=[]
            active_count=0
            for credential in credentials[:5]:
                customer=await self.session.get(User,credential.user_id);active=credential.status=='active' and credential.expires_at.replace(tzinfo=timezone.utc)>now();active_count+=int(active)
                name=(customer.display_name or customer.username or str(customer.telegram_id)) if customer else credential.remote_username
                label=f'{"🟢" if active else "🔴"} {name[:28]} · {customer.telegram_id if customer else credential.remote_username}'
                row=[(label[:64],f'admin:p:cred:{credential.id}')]
                if customer: row.append(('💬',f'url:tg://user?id={customer.telegram_id}'))
                rows.append(row)
                lines.append(f'{"🟢" if active else "🔴"} {escape(name)}\nID: {customer.telegram_id if customer else credential.remote_username} · соединений: {credential.current_connections} · IP: {credential.active_unique_ips}')
            if offset: rows.append([('← Предыдущие',f'admin:p:users:{item.id}:{max(0,offset-5)}')])
            if len(credentials)>5: rows.append([('Следующие →',f'admin:p:users:{item.id}:{offset+5}')])
            rows.append([('← К серверу',f'admin:p:server:{item.id}')])
            text=f'👥 <b>{escape(item.name)} · ключи и клиенты</b>\n\n🔑 Показано: {min(15,len(credentials))}\n🟢 Активно на странице: {active_count}\n👥 Всего пользователей Telemt: {item.remote_users}\n\n'+('\n\n'.join(lines) if lines else 'Бот ещё не выдал ни одного ключа. Удалённые пользователи Telemt без привязки к клиенту учитываются в общей статистике сервера.')
            return Screen(text,rows,rich=rich_stats(f'{item.name} · ключи',[('Показано',min(15,len(credentials))),('Активно на странице',active_count),('Всего Telemt',item.remote_users),('Соединения',item.current_connections),('Трафик',traffic(item.total_octets))]))
        if admin and page in ('move','moveapply'):
            credential=await self.session.get(ProxyCredential,int(p[1]))
            if not credential or credential.status in ('deleted','migrated'):
                raise ValueError('Этот прокси-ключ уже удалён или перенесён.')
            source=await self.server(credential.server_id);customer=await self.session.get(User,credential.user_id)
            if page=='moveapply':
                target=await self.server(p[2])
                new_credential,_,revoked=await self.move_credential(credential,target,user)
                result=await self.render(f'admin:p:cred:{new_credential.id}',user)
                result.text=('✅ Ключ перенесён. Старый доступ отозван.\n\n' if revoked else '⚠️ Новый ключ выдан, но старый сервер не подтвердил отзыв. Когда сервер восстановится, заблокируйте старый ключ вручную.\n\n')+result.text
                return result
            targets=await self.migration_targets(source,customer)
            rows=[[(f'➡️ {item.name} · {AUDIENCES.get(item.audience,item.audience)}',f'admin:p:moveapply:{credential.id}:{item.id}')] for item in targets]
            text=f'🔀 <b>Перенос прокси-ключа #{credential.id}</b>\n\nСтарый сервер: {escape(source.name)}\nКлиент: {customer.telegram_id if customer else "—"}\n\nСначала бот выдаст доступ на новом сервере и только затем попробует отозвать старый.'
            if not targets: text+='\n\nНет другого активного сервера подходящей группы.'
            return Screen(text,rows+[[('← К ключу',f'admin:p:cred:{credential.id}')]])
        if admin and page in ('moveall','moveallconfirm','moveallapply'):
            source=await self.server(p[1])
            if page=='moveallapply':
                target=await self.server(p[2]);credentials=list(await self.session.scalars(select(ProxyCredential).where(
                    ProxyCredential.server_id==source.id,
                    ProxyCredential.status.not_in(('deleted','migrated')),
                    ProxyCredential.expires_at>now(),
                ).order_by(ProxyCredential.id).limit(500)))
                moved=revoked=failed=0
                for credential in credentials:
                    try:
                        _,_,old_revoked=await self.move_credential(credential,target,user)
                        moved+=1;revoked+=int(old_revoked)
                    except ValueError:
                        failed+=1
                if moved and not failed: source.status='disabled'
                return Screen(f'✅ <b>Перенос завершён</b>\n\nСервер: {escape(source.name)} → {escape(target.name)}\nПеренесено: {moved}\nСтарый доступ подтверждённо отозван: {revoked}\nОшибок: {failed}\n\nИсходный сервер {"выключен для новой выдачи" if moved and not failed else "оставлен в прежнем состоянии"}.',[[('👥 Ключи нового сервера',f'admin:p:users:{target.id}:0')],[('← К серверам','admin:p:home')]])
            if page=='moveallconfirm':
                target=await self.server(p[2])
                count=await self.session.scalar(select(func.count()).select_from(ProxyCredential).where(ProxyCredential.server_id==source.id,ProxyCredential.status.not_in(('deleted','migrated')),ProxyCredential.expires_at>now()))
                return Screen(f'⚠️ Перенести {count} действующих ключей с «{escape(source.name)}» на «{escape(target.name)}»?\n\nКаждый новый ключ будет создан до попытки отзыва старого.',[[('✅ Начать перенос',f'admin:p:moveallapply:{source.id}:{target.id}')],[('← Отмена',f'admin:p:server:{source.id}')]],button_options={f'admin:p:moveallapply:{source.id}:{target.id}':{'style':'danger'}})
            targets=await self.migration_targets(source)
            rows=[[(f'➡️ {item.name} · {AUDIENCES.get(item.audience,item.audience)}',f'admin:p:moveallconfirm:{source.id}:{item.id}')] for item in targets]
            text=f'🔀 <b>Массовый перенос с {escape(source.name)}</b>\n\nВыберите новый сервер. Клиенты неподходящей группы будут отмечены как ошибки и останутся на старом сервере.'
            if not targets: text+='\n\nНет другого активного Telemt-сервера.'
            return Screen(text,rows+[[('← К серверу',f'admin:p:server:{source.id}')]])
        if admin and page in ('cred','credsync','credtoggle','creddeleteconfirm','creddelete'):
            credential=await self.session.get(ProxyCredential,int(p[1]))
            if not credential: raise ValueError('Ключ прокси не найден.')
            item=await self.server(credential.server_id);customer=await self.session.get(User,credential.user_id);api=self.api(item)
            if page=='credsync':
                try:
                    row=await api.user(credential.remote_username)
                    credential.current_connections=int(row.get('current_connections') or 0);credential.active_unique_ips=int(row.get('active_unique_ips') or 0);credential.total_octets=int(row.get('total_octets') or 0);credential.status='active' if row.get('enabled',True) else 'disabled';credential.last_sync=now()
                    await self.repo.set_setting(f'proxy_stats_seen:{credential.id}','1')
                    link=self.choose_link(row)
                    if link: credential.link=self.encrypt(link)
                except TelemtError as error: raise ValueError(str(error)) from error
            if page=='credtoggle':
                try:
                    if credential.status=='disabled': await api.enable_user(credential.remote_username);credential.status='active'
                    else: await api.disable_user(credential.remote_username);credential.status='disabled'
                except TelemtError as error: raise ValueError(str(error)) from error
                credential.last_sync=now();await self.repo.add(AdminAudit(actor_telegram_id=user.telegram_id,action='proxy_key_toggle',target=str(credential.id),details=credential.status))
            if page=='creddelete':
                try: await api.delete_user(credential.remote_username)
                except TelemtError as error:
                    if error.status!=404: raise ValueError(str(error)) from error
                credential.status='deleted';credential.link='';credential.current_connections=0;credential.active_unique_ips=0;credential.last_sync=now()
                await self.repo.add(AdminAudit(actor_telegram_id=user.telegram_id,action='proxy_key_delete',target=str(credential.id),details=credential.remote_username))
                return await self.render(f'admin:p:users:{item.id}:0',user)
            expiry=credential.expires_at.replace(tzinfo=timezone.utc);active=credential.status=='active' and expiry>now();status='🟢 активен' if active else '🔴 '+('истёк' if expiry<=now() else credential.status)
            text=f'🔑 <b>Прокси-ключ #{credential.id}</b>\n\nКлиент: {customer.telegram_id if customer else "—"} {"@"+customer.username if customer and customer.username else ""}\nTelemt: <code>{escape(credential.remote_username)}</code>\nСервер: {escape(item.name)}\nСтатус: {status}\nДо: {expiry:%d.%m.%Y %H:%M} UTC\nСоединений: {credential.current_connections}\nУникальных IP: {credential.active_unique_ips}\nТрафик: {traffic(credential.total_octets)}\nСинхронизация: {credential.last_sync.strftime("%d.%m.%Y %H:%M UTC") if credential.last_sync else "—"}'
            rows=[[('🔄 Обновить',f'admin:p:credsync:{credential.id}')],[('🟢 Разблокировать' if credential.status=='disabled' else '🔴 Заблокировать',f'admin:p:credtoggle:{credential.id}')],[('🔀 Перенести на другой сервер',f'admin:p:move:{credential.id}')]]
            if customer: rows.append([('💬 Связаться с клиентом',f'url:tg://user?id={customer.telegram_id}')])
            rows += [[('❌ Подтвердить удаление',f'admin:p:creddelete:{credential.id}')]] if page=='creddeleteconfirm' else [[('🗑 Удалить ключ',f'admin:p:creddeleteconfirm:{credential.id}')]]
            rows.append([('← К списку',f'admin:p:users:{item.id}:0')])
            options={f'admin:p:credtoggle:{credential.id}':{'style':'success' if credential.status=='disabled' else 'danger'},f'admin:p:creddelete:{credential.id}':{'style':'danger'},f'admin:p:credsync:{credential.id}':{'style':'primary'}}
            pairs=[('Статус',status),('Клиент',customer.telegram_id if customer else '—'),('Соединения',credential.current_connections),('Уникальные IP',credential.active_unique_ips),('Трафик',traffic(credential.total_octets)),('Действует до',expiry.strftime('%d.%m.%Y %H:%M UTC'))]
            return Screen(text,rows,button_options=options,rich=rich_stats(f'Прокси-ключ #{credential.id}',pairs))
        if admin and page in ('item','toggle','deleteconfirm','delete'):
            item=await self.endpoint(p[1])
            if page=='toggle': item.status='disabled' if item.status=='active' else 'active'
            if page=='delete': item.status='deleted';return await self.render('admin:p:home',user)
            unique=await self.session.scalar(select(func.count()).select_from(ProxyVisit).where(ProxyVisit.proxy_id==item.id));requests=await self.session.scalar(select(func.coalesce(func.sum(ProxyVisit.requests),0)).where(ProxyVisit.proxy_id==item.id));text=f'<b>{escape(item.name)}</b>\nСтарая общая ссылка: {escape(item.host)}:{item.port}\nСостояние: {item.status}\nПолучили ссылку: {unique}\nВыдач: {requests}\n\nЭта запись не связана с Telemt API и не поддерживает персональный отзыв.';rows=[[('✏️ Название',f'admin:p:input:name:{item.id}')],[('🔧 Адрес / secret',f'admin:p:input:connection:{item.id}')],[('Включить / выключить',f'admin:p:toggle:{item.id}')]];rows += [[('✅ Удалить кнопку',f'admin:p:delete:{item.id}')]] if page=='deleteconfirm' else [[('🗑 Удалить',f'admin:p:deleteconfirm:{item.id}')]];return Screen(text,rows+[back])
        if not admin and page=='open':
            item=await self.endpoint(p[1]);await self.session.execute(update(User).where(User.id==user.id).values(status=User.status))
            if item.status!='active' or not await self.eligible(user): raise ValueError('Подписка или доступ изменились. Ссылка не выдана.')
            visit=await self.session.scalar(select(ProxyVisit).where(ProxyVisit.proxy_id==item.id,ProxyVisit.user_id==user.id))
            if visit: visit.requests+=1;visit.last_at=now()
            else: await self.repo.add(ProxyVisit(proxy_id=item.id,user_id=user.id))
            return Screen(f'🌐 <b>{escape(item.name)}</b>\n\nЭто старая общая ссылка. После подключения используйте персональный сервер Telemt, когда он появится.',[[('🔌 Подключить в Telegram','url:'+self.legacy_url(item))],back])
        if admin and page=='input':
            kind=p[1]
            if kind in ('servername','serverpriority','serverlimits','serverapi'):
                item=await self.server(p[2])
                prompts={
                    'servername':f'Текущее название: {item.name}\n\nВведите новое понятное название сервера (до 80 символов).',
                    'serverpriority':f'Текущий приоритет: {item.priority}\n\nВведите число от 1 до 999. Чем меньше число, тем раньше сервер показывается клиенту.',
                    'serverlimits':f'Текущие лимиты: {item.unique_ip_limit} IP · {item.tcp_connection_limit} TCP\n\nВведите: лимит IP | лимит TCP\nНапример: 2 | 2',
                    'serverapi':f'Текущий API: {item.api_url}\n\nВведите: API URL | Authorization\nСекретное сообщение будет удалено после обработки.',
                }
                return Screen(prompts[kind],[[('← К настройкам',f'admin:p:settings:{item.id}')]])
            if kind in ('server','serveredit'):
                wizard={'kind':kind,'item':p[2] if kind=='serveredit' and len(p)>2 else '', 'values':[]}
                await self.repo.set_setting(f'proxy_wizard:{user.telegram_id}',json.dumps(wizard))
                return Screen(self.SERVER_STEPS[0],[back])
            prompts={'grant':'Введите Telegram ID | дней | лимит IP | лимит TCP. Например: 123456789 | 30 | 2 | 10','price':'Введите новую цену за 30 дней в рублях, например 30 или 49.90.','add':'Введите название | ссылку MTProxy (устаревший режим).','name':'Введите новое название (до 80 символов).','connection':'Пришлите новую ссылку https://t.me/proxy?server=...&port=...&secret=...'};return Screen(prompts[kind],[back])
        raise ValueError('Раздел недоступен.')

    @staticmethod
    def validate_api_url(value):
        parsed=urlparse(value)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'): raise ValueError('API URL должен иметь вид https://host:9091 без логина, пути и параметров.')
        if parsed.scheme=='http':
            try: private=ip_address(parsed.hostname).is_private
            except ValueError: private=False
            if not private: raise ValueError('HTTP разрешён только для приватного IP. Для публичного адреса используйте HTTPS.')
        return value.rstrip('/')
    async def save_server(self,text,item=None):
        parts=[part.strip() for part in text.split('|')]
        if len(parts)!=7: raise ValueError('Нужно 7 полей: название | API URL | группа | приоритет | лимит IP | лимит TCP | Authorization.')
        name,url,audience,priority,limit,tcp_limit,authorization=parts
        if not 1<=len(name)<=80 or audience not in AUDIENCES or not priority.isdigit() or not 1<=int(priority)<=999 or not limit.isdigit() or not 2<=int(limit)<=100 or not tcp_limit.isdigit() or not 2<=int(tcp_limit)<=1000 or not authorization or len(authorization)>1000: raise ValueError('Проверьте название, группу vpn/paid/all, приоритет 1–999, лимит IP 2–100, TCP 2–1000 и Authorization.')
        url=self.validate_api_url(url)
        duplicate=await self.session.scalar(select(ProxyServer).where(ProxyServer.api_url==url))
        if duplicate and not item and duplicate.status=='deleted':
            # Restore the soft-deleted row so the unique URL, issued-key history
            # and audit references remain consistent.
            item=duplicate
        elif duplicate and (not item or duplicate.id!=item.id):
            raise ValueError('Этот API URL уже подключён как другой Telemt-сервер.')
        try: await TelemtClient(url,authorization).health()
        except TelemtError as error: raise ValueError(f'Не удалось подключиться к Telemt API: {error}') from error
        if item: item.name=name;item.api_url=url;item.auth=self.encrypt(authorization);item.audience=audience;item.priority=int(priority);item.unique_ip_limit=int(limit);item.tcp_connection_limit=int(tcp_limit);item.status='active';item.health='online';item.last_error='';item.last_sync=now()
        else: item=await self.repo.add(ProxyServer(name=name,api_url=url,auth=self.encrypt(authorization),audience=audience,priority=int(priority),unique_ip_limit=int(limit),tcp_connection_limit=int(tcp_limit),health='online',last_sync=now()))
        return item

    async def provision_existing(self,item):
        """Issue access already paid before this server was connected."""
        issued=0
        if item.audience in ('paid','all'):
            rows=await self.session.scalars(select(AppSetting).where(AppSetting.key.startswith('proxy_paid_until:')).limit(200))
            for row in rows:
                try:
                    user_id=int(row.key.rsplit(':',1)[1]);expiry=datetime.fromisoformat(row.value)
                except (ValueError,IndexError):
                    continue
                if expiry<=now(): continue
                customer=await self.session.get(User,user_id)
                if not customer or customer.status=='blocked': continue
                try:
                    await self.provision(item,customer,expiry,
                        await self.repo.setting(f'proxy_limit:{customer.id}','2'),
                        await self.repo.setting(f'proxy_tcp_limit:{customer.id}','2'),'paid')
                    issued+=1
                except ValueError:
                    continue
        if item.audience in ('vpn','all'):
            user_ids=await self.session.scalars(select(Subscription.user_id).where(Subscription.expires_at>now()).distinct().limit(200))
            for user_id in user_ids:
                customer=await self.session.get(User,user_id)
                if not customer or customer.status=='blocked': continue
                expiry=await self.access_expiry(customer,'vpn')
                if not expiry: continue
                try:
                    await self.provision(item,customer,expiry,
                        await self.repo.setting(f'proxy_limit:{customer.id}','2'),
                        await self.repo.setting(f'proxy_tcp_limit:{customer.id}','2'),'vpn');issued+=1
                except ValueError:
                    continue
        return issued

    async def input(self,action,user,text):
        self.admin(user,action);p=action.split(':');kind=p[3];text=text.strip()
        if kind=='price':
            try:
                price=Decimal(text.replace(',','.'))*100
                if not price.is_finite() or price!=price.to_integral_value() or not 1<=price<=100000000: raise ValueError('Цена должна быть от 0.01 до 1 000 000 ₽, до двух знаков после запятой.')
            except InvalidOperation: raise ValueError('Введите цену числом.')
            await self.repo.set_setting('proxy_monthly_price',int(price));return await self.render('admin:p:home',user)
        if kind in ('servername','serverpriority','serverlimits','serverapi'):
            item=await self.server(p[4])
            if kind=='servername':
                if not 1<=len(text)<=80: raise ValueError('Название должно содержать от 1 до 80 символов.')
                old=item.name;item.name=text;details=f'{old}->{text}'
            elif kind=='serverpriority':
                if not text.isdigit() or not 1<=int(text)<=999: raise ValueError('Приоритет: целое число от 1 до 999.')
                old=item.priority;item.priority=int(text);details=f'{old}->{item.priority}'
            elif kind=='serverlimits':
                values=[value.strip() for value in text.split('|')]
                if len(values)!=2 or not all(value.isdigit() for value in values) or not 2<=int(values[0])<=100 or not 2<=int(values[1])<=1000: raise ValueError('Формат: лимит IP 2–100 | лимит TCP 2–1000.')
                old=f'{item.unique_ip_limit}/{item.tcp_connection_limit}';item.unique_ip_limit=int(values[0]);item.tcp_connection_limit=int(values[1]);details=f'{old}->{values[0]}/{values[1]}'
            else:
                values=[value.strip() for value in text.split('|',1)]
                if len(values)!=2 or not values[1] or len(values[1])>1000: raise ValueError('Формат: API URL | Authorization.')
                url=self.validate_api_url(values[0])
                duplicate=await self.session.scalar(select(ProxyServer).where(ProxyServer.api_url==url,ProxyServer.id!=item.id))
                if duplicate: raise ValueError('Этот API URL уже используется другим Telemt-сервером.')
                try: await TelemtClient(url,values[1]).health()
                except TelemtError as error: raise ValueError(f'Не удалось подключиться к Telemt API: {error}') from error
                old=item.api_url;item.api_url=url;item.auth=self.encrypt(values[1]);item.health='online';item.last_error='';item.last_sync=now();details=f'{old}->{url}'
            await self.repo.add(AdminAudit(actor_telegram_id=user.telegram_id,action='proxy_server_'+kind,target=str(item.id),details=details))
            return await self.render(f'admin:p:settings:{item.id}',user)
        if kind in ('server','serveredit'):
            wizard_key=f'proxy_wizard:{user.telegram_id}'
            if text.count('|')==6:
                item=await self.save_server(text,await self.server(p[4]) if kind=='serveredit' else None)
            else:
                try: wizard=json.loads(await self.repo.setting(wizard_key,'{}'))
                except json.JSONDecodeError: wizard={}
                values=wizard.get('values') if wizard.get('kind')==kind else None
                if not isinstance(values,list) or len(values)>=7:
                    values=[]
                step=len(values)
                if step==0 and not 1<=len(text)<=80: raise ValueError('Название должно содержать от 1 до 80 символов.')
                if step==1: self.validate_api_url(text)
                if step==2 and text not in AUDIENCES: raise ValueError('Введите vpn, paid или all.')
                ranges={3:(1,999,'Приоритет'),4:(2,100,'Лимит IP'),5:(2,1000,'Лимит TCP')}
                if step in ranges:
                    low,high,label=ranges[step]
                    if not text.isdigit() or not low<=int(text)<=high: raise ValueError(f'{label}: целое число от {low} до {high}.')
                if step==6 and (not text or len(text)>1000): raise ValueError('Код Authorization пустой или слишком длинный.')
                values.append(text)
                if len(values)<7:
                    wizard.update(kind=kind,item=wizard.get('item',''),values=values)
                    await self.repo.set_setting(wizard_key,json.dumps(wizard))
                    return Screen(self.SERVER_STEPS[len(values)],[[('← Отменить','admin:p:home')]],keep_input=True)
                item_id=wizard.get('item') or (p[4] if kind=='serveredit' and len(p)>4 else '')
                item=await self.save_server(' | '.join(values),await self.server(item_id) if kind=='serveredit' else None)
            await self.repo.set_setting(wizard_key,'')
            issued=await self.provision_existing(item)
            await self.repo.add(AdminAudit(actor_telegram_id=user.telegram_id,action='proxy_server_save',target=str(item.id),details=f'{item.name}; {item.audience}; priority {item.priority}; issued {issued}'))
            return await self.render(f'admin:p:server:{item.id}',user)
        if kind=='grant':
            parts=[part.strip() for part in text.split('|')]
            if len(parts)!=4 or not all(part.isdigit() for part in parts): raise ValueError('Формат: Telegram ID | дней | лимит IP | лимит TCP.')
            telegram_id,days,limit,tcp_limit=map(int,parts)
            if not 1<=days<=3650 or not 2<=limit<=100 or not 2<=tcp_limit<=1000: raise ValueError('Срок 1–3650 дней, лимит IP 2–100, TCP 2–1000.')
            customer=await self.repo.user(telegram_id)
            if not customer: raise ValueError('Клиент ещё не запускал бота.')
            raw=await self.repo.setting(f'proxy_paid_until:{customer.id}',now().isoformat());until=max(now(),datetime.fromisoformat(raw))+timedelta(days=days);await self.repo.set_setting(f'proxy_paid_until:{customer.id}',until.isoformat());await self.repo.set_setting(f'proxy_limit:{customer.id}',str(limit));await self.repo.set_setting(f'proxy_tcp_limit:{customer.id}',str(tcp_limit));issued=0
            for server in await self.matching_servers('paid'):
                try: await self.provision(server,customer,until,limit,tcp_limit,'paid');issued+=1
                except ValueError: pass
            await self.repo.add(AdminAudit(actor_telegram_id=user.telegram_id,action='proxy_grant',target=str(customer.id),details=f'{days} days; {limit} IP; {tcp_limit} TCP; {issued} servers'));return Screen(f'✅ Клиенту {telegram_id} выдан прокси на {days} дней. Лимиты: {limit} IP, {tcp_limit} TCP. Выпущено на серверах: {issued}.',[[('← Назад','admin:home' if self.settings.role(user.telegram_id)=='manager' else 'admin:p:home')]])
        if kind=='name':
            if not 1<=len(text)<=80: raise ValueError('Название: 1–80 символов.')
            item=await self.endpoint(p[4]);item.name=text
        elif kind in ('add','connection'):
            name,link=(text.split('|',1) if '|' in text else ('',text))
            if kind=='add' and not 1<=len(name.strip())<=80: raise ValueError('Нужен формат: название | ссылка MTProxy.')
            parsed=urlparse(link.strip());valid=(parsed.scheme=='https' and parsed.netloc=='t.me' and parsed.path=='/proxy') or (parsed.scheme=='tg' and parsed.netloc=='proxy');data=parse_qs(parsed.query)
            if not valid or set(data)!={'server','port','secret'} or any(len(v)!=1 for v in data.values()): raise ValueError('Нужна ссылка t.me/proxy или tg://proxy с server, port и secret.')
            host,port,secret=data['server'][0].strip(),data['port'][0],data['secret'][0].lower()
            try: ip_address(host)
            except ValueError:
                if not re.fullmatch(r'(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?',host) or '..' in host: raise ValueError('Неверный адрес сервера.')
            if not port.isdigit() or not 1<=int(port)<=65535 or not re.fullmatch(r'(?:[a-f0-9]{32}|dd[a-f0-9]{32}|ee[a-f0-9]{32}(?:[a-f0-9]{2}){1,253})',secret): raise ValueError('Проверьте порт и hex secret MTProxy.')
            encrypted=self.encrypt(secret)
            if kind=='add': item=await self.repo.add(ProxyEndpoint(name=name.strip(),host=host,port=int(port),secret=encrypted))
            else: item=await self.endpoint(p[4]);item.host=host;item.port=int(port);item.secret=encrypted
        else: raise ValueError('Действие недоступно.')
        return await self.render(f'admin:p:item:{item.id}',user)


class ProxyCheckoutService:
    """Direct YooKassa checkout for a personal Telemt proxy."""
    def __init__(self,repo,settings,provider=None):
        self.repo,self.settings=repo,settings
        if provider is None:
            from app.services.topups import YooKassa
            provider=YooKassa(settings)
        self.provider=provider

    @property
    def enabled(self):
        return bool(self.settings.yookassa_shop_id and self.settings.yookassa_secret_key)

    async def prepare(self,user,return_url=None,new_connection=False):
        proxy=ProxyService(self.repo,self.settings)
        if not new_connection and await proxy.access_kind(user)=='vpn':
            return Screen('Telegram-прокси уже доступен бесплатно по активной VPN-подписке.',[[('🌐 Открыть прокси','p:home')],[('← Назад','home')]])
        if not self.enabled or await self.repo.setting('payment_provider:yookassa','1')!='1':
            return Screen('Оплата картой и СБП сейчас недоступна. Используйте баланс или обратитесь в поддержку.',[[('💰 Оплатить с баланса','p:buy')],[('← Назад','p:home')]])
        from app.services.proxy_control import plans
        from app.web.proxy_control import purchase,Purchase
        plan=next((p for p in await plans(self.repo) if p.enabled),None)
        if not plan:raise ValueError('Тарифы Proxy недоступны')
        x=await purchase(self.repo,self.settings,user,Purchase(method='yookassa',plan_id=plan.id,request_id=str(uuid.uuid4())))
        return Screen(f'Заказ Telegram Proxy · {plan.days} дней · {money(x["amount"])}',[[('Оплатить','url:'+x['redirect_url'])],[('Проверить оплату',f'p:cardcheck:{x["payment_id"]}')],[('Назад','p:home')]])

    async def details(self,payment):
        try: data=json.loads(await self.repo.setting(f'proxy_order:{payment.id}','{}'))
        except json.JSONDecodeError: data={}
        if data.get('version')==2:
            if data.get('price')!=payment.amount or not 1<=int(data.get('days',0))<=3650 or not 1<=int(data.get('limit',0))<=100:raise ValueError('Данные заказа прокси повреждены')
            return data
        if data.get('price')!=payment.amount or data.get('days')!=30 or data.get('limit')!=2:
            raise ValueError('Данные заказа прокси повреждены. Обратитесь в поддержку.')
        return data

    async def credit(self,payment,data):
        if payment.status=='proxy_order_paid': return True
        if data.get('id')!=payment.provider_id: raise ValueError('Не совпадает идентификатор платежа.')
        if data.get('status')=='canceled': payment.status='proxy_order_canceled';return False
        if data.get('status')!='succeeded' or data.get('paid') is not True: return False
        order=await self.details(payment);amount=data.get('amount',{});meta=data.get('metadata',{})
        expected={'freeman_payment':str(payment.id),'freeman_user':str(payment.user_id),'freeman_kind':'proxy','freeman_days':str(order['days']),'freeman_limit':str(order['limit'])}
        if amount.get('currency')!='RUB' or Decimal(amount.get('value','0'))*100!=payment.amount or any(meta.get(key)!=value for key,value in expected.items()) or bool(data.get('test',False))!=self.settings.yookassa_test_mode:
            raise ValueError('Данные платежа не прошли проверку. Обратитесь в поддержку.')
        claimed=await self.repo.session.execute(update(Payment).where(Payment.id==payment.id,Payment.status=='proxy_order_pending').values(status='proxy_order_processing'))
        if not claimed.rowcount:
            await self.repo.session.refresh(payment)
            return payment.status=='proxy_order_paid'
        user=await self.repo.session.get(User,payment.user_id)
        if order.get('version')==2:
            from app.web.proxy_control import fulfill_v2
            c=await fulfill_v2(self.repo,self.settings,payment,order)
            payment.status='proxy_order_paid'
            return True
        raw=await self.repo.setting(f'proxy_paid_until:{user.id}')
        try: current=datetime.fromisoformat(raw) if raw else now()
        except ValueError: current=now()
        until=(now() if order.get('new_connection') else max(now(),current))+timedelta(days=order['days'])
        await self.repo.set_setting(f'proxy_paid_until:{user.id}',max(now(),current,until).isoformat())
        await self.repo.set_setting(f'proxy_limit:{user.id}','2')
        await self.repo.set_setting(f'proxy_tcp_limit:{user.id}','2')
        issued=0;proxy=ProxyService(self.repo,self.settings)
        for server in await proxy.matching_servers('paid'):
            try:
                await proxy.provision(server,user,until,2,2,'paid',connection_token=f'pay{payment.id}' if order.get('new_connection') else None);issued+=1
                if order.get('new_connection'): break
            except ValueError: pass
        payment.status='proxy_order_paid'
        from app.web.admin_reports import record_sale
        await record_sale(self.repo,payment,1,order['limit'],kind='proxy',renewal=not order.get('new_connection',False))
        text=f'✅ Оплата {money(payment.amount)} подтверждена. Telegram-прокси активен до {until:%d.%m.%Y %H:%M} UTC. Лимит: 2 устройства.'
        if not issued: text+=' Сервер Telemt ещё не подключён; оплаченный срок сохранён и ссылка появится после подключения.'
        if user.telegram_id:
            await self.repo.add(ChatNotice(dedup=f'proxy_order:{payment.id}',target_id=user.telegram_id,kind='client',text=text,buttons=json.dumps([[['🌐 Получить прокси','p:home']]])))
        return True

    async def success(self,payment,user):
        order=await self.details(payment)
        if order.get('version')==2:
            from app.database.models import ProxyAccessDetails
            c=await self.repo.session.scalar(select(ProxyCredential).join(ProxyAccessDetails,ProxyAccessDetails.credential_id==ProxyCredential.id).where(ProxyAccessDetails.payment_id==payment.id))
            if c:return Screen(f'✅ Оплата подтверждена. Telegram Proxy до {c.expires_at:%d.%m.%Y}. Лимит: {order["limit"]} IP.',[[('Подключить в Telegram','url:'+ProxyService(self.repo,self.settings).decrypt(c.link))],[('Мои подключения','p:home')]])
        raw=await self.repo.setting(f'proxy_paid_until:{user.id}')
        until=datetime.fromisoformat(raw) if raw else now()
        return Screen(f'✅ Оплата подтверждена. Персональный Telegram-прокси активен до {until:%d.%m.%Y %H:%M} UTC.\n\nЛимит: 2 устройства, 2 уникальных IP и 2 одновременных подключения.',[[('🌐 Получить прокси','p:home')],[('← Назад','home')]])

    async def render(self,action,user):
        p=action.split(':')
        if p[:2]==['p','card'] and len(p)==2:
            return await self.prepare(user,new_connection=True)
        if len(p)!=3 or p[1] not in ('cardpay','cardcheck'):
            raise ValueError('Платёж прокси недоступен.')
        payment=await self.repo.session.get(Payment,int(p[2]),populate_existing=True)
        if not payment or payment.user_id!=user.id or not payment.status.startswith('proxy_order_'):
            raise ValueError('Платёж прокси недоступен.')
        if payment.status=='proxy_order_paid': return await self.success(payment,user)
        if payment.status=='proxy_order_canceled': return Screen('Платёж отменён. Доступ не изменён.',[[('💳 Попробовать снова','p:card')],[('← Назад','p:home')]])
        order=await self.details(payment)
        if not payment.provider_id:
            if payment.created_at.replace(tzinfo=timezone.utc)<now()-timedelta(hours=23): raise ValueError('Счёт устарел. Создайте новый.')
            metadata={'freeman_kind':'proxy','freeman_days':str(order['days']),'freeman_limit':str(order['limit'])}
            if order.get('return_url'):
                data=await self.provider.create(payment,await self.repo.setting(f'proxy_order_idem:{payment.id}'),f'Telegram-прокси F R E E M A N · {order["days"]} дней #{payment.id}',metadata,return_url=order['return_url'])
            else:
                data=await self.provider.create(payment,await self.repo.setting(f'proxy_order_idem:{payment.id}'),f'Telegram-прокси F R E E M A N · {order["days"]} дней #{payment.id}',metadata)
            payment.provider_id=data['id'];payment.status='proxy_order_pending'
            url=data.get('confirmation',{}).get('confirmation_url','')
            if not url.startswith('https://'): raise ValueError('ЮKassa не вернула ссылку оплаты.')
            await self.repo.set_setting(f'proxy_order_url:{payment.id}',url)
        if p[1]=='cardcheck' and await self.credit(payment,await self.provider.get(payment.provider_id)):
            return await self.success(payment,user)
        url=await self.repo.setting(f'proxy_order_url:{payment.id}')
        return Screen(f'Заказ #{payment.id}: персональный Telegram-прокси на {order["days"]} дней за {money(payment.amount)}.\nЛимит: {order["limit"]} IP. После оплаты доступ активируется автоматически.',[[('💳 Оплатить картой / СБП','url:'+url)],[('🔄 Проверить оплату',f'p:cardcheck:{payment.id}')],[('← Назад','p:home')]])

class ProxyServerWorker:
    def __init__(self,database,settings): self.database,self.settings=database,settings
    async def once(self):
        import time,logging
        started=time.perf_counter()
        from app.database.repositories import SQLRepository
        async with self.database.sessions() as session:
            ids=list(await session.scalars(select(ProxyServer.id).where(ProxyServer.status!='deleted').order_by(ProxyServer.id)))
        # Commit telemetry before moving to another remote server, and before
        # taking customer locks. A slow server must not retain earlier rows.
        from app.services.proxy_control import worker_transaction,maintenance_worker
        for sid in ids:
            try:
                async with self.database.sessions.begin() as session:
                    await worker_transaction(session)
                    server=await session.get(ProxyServer,sid)
                    if server and server.status!='deleted':
                        await ProxyService(SQLRepository(session),self.settings,api_timeout=3).refresh_server(server)
            except Exception as error:
                import logging
                logging.getLogger(__name__).warning('Proxy telemetry transaction postponed: %s',type(error).__name__)
        await maintenance_worker(self.database,self.settings)
        logging.getLogger(__name__).info('Proxy synchronization pass finished: servers=%s elapsed_ms=%.1f',len(ids),(time.perf_counter()-started)*1000)
    async def run(self):
        import asyncio,logging
        while True:
            try: await self.once()
            except asyncio.CancelledError: raise
            except Exception: logging.getLogger(__name__).exception('Proxy server sync failed')
            await asyncio.sleep(120)
