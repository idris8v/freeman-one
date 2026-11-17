from app.services.subscription_labels import expiry_date
"""Partner-only exact-key lookup and confirmed device unlink; no admin delegation."""
import json,re
from html import escape
from types import SimpleNamespace
from urllib.parse import urlsplit,urlunsplit,unquote
from sqlalchemy import select,or_
from app.database.models import VlessCredential,Subscription,AccessKey,AdminAudit,AppSetting,now
from app.services.screens import Screen,time_remaining,duration
from app.services.bot_admin_search import identifier,canonical,BotAdminSearch
from app.services.remnawave import RemnawaveService

class PartnerService:
    def __init__(self,repo,settings):
        self.repo,self.settings,self.session=repo,settings,repo.session
        # Device renderer only needs these three methods; it never receives an
        # owner identity or a settings object with elevated permissions.
        panel=RemnawaveService(repo,settings)
        self.service=SimpleNamespace(admin=self.require,server=panel.server,api=panel.api)
        self.actor=None
    def require(self,actor):
        if self.settings.role(actor.telegram_id)!='partner' or actor.status=='blocked':
            raise PermissionError('Доступ партнёра недоступен.')
    async def authorize(self,actor):
        self.require(actor)
        role=f'staff_role:{actor.telegram_id}'
        blocked=f'staff_blocked:{actor.telegram_id}'
        flags=dict((await self.session.execute(select(AppSetting.key,AppSetting.value).where(
            AppSetting.key.in_((role,blocked))))).all())
        if flags.get(role)!='partner' or flags.get(blocked)=='1':
            raise PermissionError('Доступ партнёра закрыт.')
        self.actor=actor
    def home(self):
        return Screen('🤝 <b>Партнёр</b>\n\n🔑 <b>Получить ключ</b>\nВыберите устройства и срок. Цена начисляется в долг после выдачи.\n\n🔎 <b>Найти ключ</b>\nПришлите ссылку подписки или VLESS-ключ, чтобы посмотреть или отвязать устройство.',[[('🔑 Получить ключ','partner:plans')],[('🔎 Найти ключ','partner:search')],[('📦 Мои ключи','subscriptions')],[('💼 Расчёты','partner:ledger')]],structured=True)
    async def find(self,value,actor):
        await self.authorize(actor)
        if not value or len(value)>8192:raise ValueError('Пришлите ссылку подписки или VLESS-ключ.')
        kind,term=identifier(value);ids=[]
        if kind=='url':
            u=urlsplit(term);term=urlunsplit((u.scheme,u.netloc,u.path,u.query,''));norm=canonical(term)
            if not norm:raise ValueError('Некорректная ссылка подписки.')
            tail=unquote(u.path.rstrip('/').rsplit('/',1)[-1])
            for c in await self.session.scalars(select(VlessCredential).where(VlessCredential.status!='deleted',or_(VlessCredential.subscription_url==term,VlessCredential.short_uuid==tail))):
                if canonical(c.subscription_url)==norm:ids.append(c.id)
        elif kind=='vless':
            ids=[cid for _,cid in await BotAdminSearch(self.repo,self.settings).remote_vless(term)]
        else:raise ValueError('Поиск партнёра работает по полной ссылке подписки или VLESS-ключу. Поиск клиентов по имени, номеру или почте недоступен.')
        ids=list(dict.fromkeys(ids))
        await self.repo.set_setting(f'partner_keys:{actor.id}',json.dumps(dict(ids=ids,expires=now().timestamp()+1800)))
        if len(ids)==1:return await self.render(f'partner:key:{ids[0]}',actor)
        return await self.render('partner:results:0',actor)
    async def scope(self):
        try:data=json.loads(await self.repo.setting(f'partner_keys:{self.actor.id}','{}'))
        except ValueError:data={}
        return data.get('ids',[]) if data.get('expires',0)>now().timestamp() else []
    async def credential(self,identity):
        if int(identity) not in await self.scope():raise ValueError('Пришлите ключ заново: результат поиска недоступен.')
        c=await self.session.get(VlessCredential,int(identity))
        if not c or c.status=='deleted':raise ValueError('Ключ не найден.')
        return c
    async def audit(self,actor,operation,target):
        await self.repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='partner_'+operation,target=str(target),details='Отвязка устройства партнёром'))
    async def render(self,action,actor):
        await self.authorize(actor)
        p=action.split(':')[1:];op=p[0]
        if op=='home':return self.home()
        if op in ('plans','terms','plan','quote','issue'):
            from app.services.partner_issuance import render
            return await render(self.repo,self.settings,actor,p)
        if op=='ledger':
            from app.services.partner_ledger import entries,statement
            monthly=len(p)>1 and p[1]=='month'
            offset_index=2 if monthly else 1
            offset=max(0,int(p[offset_index])) if len(p)>offset_index else 0
            screen=statement(await entries(self.repo,actor.id),offset,'partner:ledger','partner:home',
                             monthly=monthly)
            return screen
        if op=='search':return Screen('🔎 <b>Найти ключ</b>\n\nОтправьте полную ссылку подписки или VLESS-ключ в этот чат.\n\nПосле поиска можно проверить срок, количество подключённых устройств и отвязать отдельное устройство.',[[('← Назад','partner:home')]],structured=True)
        if op=='results':
            ids=await self.scope();offset=max(0,int(p[1])) if len(p)>1 else 0
            rows=[[(f'🔑 Подписка {offset+i+1}',f'partner:key:{cid}')] for i,cid in enumerate(ids[offset:offset+5])]
            if offset:rows.append([('← Предыдущие',f'partner:results:{max(0,offset-5)}')])
            if offset+5<len(ids):rows.append([('Следующие →',f'partner:results:{offset+5}')])
            return Screen('🔎 <b>Результат поиска</b>\n\n'+(f'Найдено: {len(ids)}' if ids else 'Ключ не найден. Проверьте ссылку.'),rows+[[('🔎 Другой поиск','partner:search')],[('← Назад','partner:home')]],structured=True)
        if op=='key':
            c=await self.credential(p[1]);sub=await self.session.get(Subscription,c.subscription_id);key=await self.session.get(AccessKey,c.access_key_id)
            tariff=await self.repo.tariff_by_id(sub.tariff_id) if sub and sub.tariff_id else None
            term=duration(tariff.months) if tariff else 'Индивидуальная подписка'
            label=await self.repo.setting(f'subscription_name:{c.subscription_id}','') or (key.name if key else 'Подписка')
            if re.match(r'^(?:vless(?:\s|·|$)|https?://)',label,re.I):label='Подписка'
            from app.services.key_device_counts import device_counts
            try:
                panel=await self.service.server(c.server_id)
                count=(await device_counts([(c.id,self.service.api(panel),c.remote_user_uuid)]))[c.id]
            except Exception:count=None
            if count is not None and c.device_limit>0:count=min(count,c.device_limit)
            bound=f'{count} из {c.device_limit}' if count is not None else f'Не удалось проверить · лимит {c.device_limit}'
            text=f'🔑 <b>{escape(label)}</b>\n\n<b>Подписка:</b> {escape(term)} · {c.device_limit} устр.\n\n<b>Подключено устройств:</b> {bound}\n\n<b>Осталось:</b> {time_remaining(c.expires_at)}\n\n<b>Действует до:</b> {expiry_date(sub.expires_at if sub else c.expires_at)}\n\n<b>Ключ</b>\n<code>{escape(c.subscription_url)}</code>'
            return Screen(text,[[('📱 Устройства',f'partner:devices:{c.id}')],[('🔄 Обновить',action)],[('🔎 Другой поиск','partner:search')],[('← Назад','partner:results:0')]],structured=True)
        if op in ('devices','device','device_confirm','device_apply'):
            from app.services.admin_devices import render
            screen=await render(self,p,actor)
            rows=[];options={}
            for row in screen.rows:
                clean=[]
                for label,target in row:
                    if not target.startswith('admin:r:'):continue
                    tail=target[len('admin:r:'):]
                    if tail.split(':')[0] not in ('key','devices','device','device_confirm','device_apply'):continue
                    mapped='partner:'+tail;clean.append((label,mapped))
                    if target in screen.button_options:options[mapped]=screen.button_options[target]
                if clean:rows.append(clean)
            screen.rows=rows;screen.button_options=options
            return screen
        raise PermissionError('Действие партнёру недоступно.')
