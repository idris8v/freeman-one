"""One-recipient proxy invitations shared by the admin website and bot."""
import hashlib,json,re,secrets
from datetime import timedelta,timezone
from sqlalchemy import update
from app.database.models import AppSetting,ProxyCredential,ProxyServer,AdminAudit,now
from app.services.proxies import ProxyService
from app.services.screens import Screen

class ProxyInvites:
    def __init__(self,repo,settings):self.repo=repo;self.settings=settings
    def key(self,token):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{20,48}',token):raise ValueError('Некорректная ссылка приглашения')
        return 'proxy_invite:'+hashlib.sha256(token.encode()).hexdigest()
    async def create(self,actor,server_id,days,ip,tcp):
        if not self.settings.is_owner(actor.telegram_id):raise PermissionError('Нет доступа')
        server=await self.repo.session.get(ProxyServer,server_id)
        if not server or server.status!='active' or server.audience not in ('all','paid'):raise ValueError('Выберите активный сервер для всех клиентов или отдельного доступа')
        if not 1<=days<=3650 or not 1<=ip<=100 or not 1<=tcp<=1000:raise ValueError('Проверьте срок и лимиты')
        token=secrets.token_urlsafe(24);key=self.key(token)
        data=dict(server_id=server_id,days=days,ip=ip,tcp=tcp,actor=actor.telegram_id,expires=(now()+timedelta(days=3)).isoformat(),status='open')
        await self.repo.set_setting(key,json.dumps(data))
        await self.repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='proxy_invite_create',target=str(server_id),details=f'{days} days; invitation expires in 3 days'))
        return dict(link=f'https://t.me/{self.settings.bot_username}?start=px_{token}',days=days,server=server.name)
    async def claim(self,token,user):
        async with self.repo.session.begin_nested():return await self._claim(token,user)
    async def _claim(self,token,user):
        if user.status=='blocked':raise ValueError('Профиль заблокирован')
        key=self.key(token);raw=await self.repo.setting(key)
        if not raw:raise ValueError('Приглашение не найдено')
        data=json.loads(raw);service=ProxyService(self.repo,self.settings)
        if data.get('user_id'):
            if data['user_id']!=user.id:raise ValueError('Это приглашение уже получил другой клиент')
            c=await self.repo.session.get(ProxyCredential,data.get('credential_id'))
            if not c or c.status!='active' or c.expires_at.replace(tzinfo=timezone.utc)<=now():raise ValueError('Этот доступ уже не активен')
            return self.screen(c,service)
        if data.get('intended_user') and data['intended_user']!=user.id:raise ValueError('Приглашение предназначено другому клиенту')
        if data['status']!='open' or data['expires']<now().isoformat():raise ValueError('Срок приглашения истёк')
        claimed={**data,'status':'claiming','user_id':user.id}
        changed=await self.repo.session.execute(update(AppSetting).where(AppSetting.key==key,AppSetting.value==raw).values(value=json.dumps(claimed)))
        if changed.rowcount!=1:raise ValueError('Приглашение уже обрабатывается. Откройте ссылку повторно')
        server=await service.server(data['server_id']);expiry=now()+timedelta(days=data['days'])
        credential,link=await service.provision(server,user,expiry,data['ip'],data['tcp'],'paid',connection_token='invite_'+hashlib.sha256(token.encode()).hexdigest()[:20])
        from app.services.proxy_control import meta
        m=await meta(self.repo,credential);m.source=data.get('source','admin');m.ip_limit=data['ip']
        old=await self.repo.setting(f'proxy_paid_until:{user.id}')
        if not old or old<expiry.isoformat():await self.repo.set_setting(f'proxy_paid_until:{user.id}',expiry.isoformat())
        claimed.update(status='claimed',credential_id=credential.id);await self.repo.set_setting(key,json.dumps(claimed))
        await self.repo.add(AdminAudit(actor_telegram_id=data['actor'],action='proxy_invite_claim',target=str(user.id),details=f'credential={credential.id}; server={server.id}'))
        return self.screen(credential,service)
    def screen(self,c,service):
        link=service.public_link(service.decrypt(c.link))
        return Screen(f'✅ <b>Ваш Telegram Proxy готов</b>\n\nДоступ до {c.expires_at:%d.%m.%Y}. Нажмите кнопку ниже — Telegram подключится к прокси. VPN для этого не нужен.',[[('Подключить Telegram Proxy','url:'+link)],[('Мои подключения','p:home')]])
