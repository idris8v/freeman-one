from app.services.subscription_labels import expiry_date
"""Owner-only Telegram management of the shared Remnawave credentials."""
import io
from base64 import urlsafe_b64decode, urlsafe_b64encode
import json
import secrets
from datetime import timedelta, timezone
from html import escape
from sqlalchemy import select, or_
from app.database.models import User, Subscription, AccessKey, VlessCredential, AdminAudit, now
from app.services.screens import Screen, time_remaining
from app.services.remnawave import RemnawaveService, RemnawaveApiError
from app.services import vless_provision
from app.services.device_reserve import technical

class RemnawaveBot:
    def __init__(self, repo, settings):
        self.repo, self.settings, self.session = repo, settings, repo.session
        self.service = RemnawaveService(repo, settings)

    async def credential(self, identity):
        c = await self.session.get(VlessCredential, int(identity))
        if not c or c.status == 'deleted': raise ValueError('VLESS-ключ не найден.')
        return c

    async def audit(self, user, operation, target):
        event=await self.repo.add(AdminAudit(actor_telegram_id=user.telegram_id, action='bot_vless_'+operation, target=str(target), details='Remnawave'))
        titles={
            'block':'🔑 Доступ по ключу приостановлен', 'unblock':'🔑 Доступ по ключу восстановлен',
            'rotate':'🔑 Ссылка на подписку обновлена', 'delete':'🔑 Ключ удалён',
            'devices':'📱 Устройства отвязаны', 'device_unlink':'📱 Устройство отвязано',
            'name':'✏️ Название ключа обновлено', 'limit':'📱 Лимит устройств изменён',
            'traffic':'📊 Параметры трафика обновлены',
        }
        if operation in titles:
            credential=await self.session.get(VlessCredential,int(target))
            if credential:
                from app.services.admin_client_notices import queue
                recipient=await self.session.get(User,credential.user_id)
                button=('Мои ключи','subscriptions') if operation=='delete' else ('Открыть подписку',f'sub:{credential.subscription_id}')
                await queue(self.repo,recipient,user,f'vless:{event.id}',titles[operation],button=button)

    async def render(self, action, user):
        from app.services.admin_key_navigation import split,decorate
        base,origin=split(action)
        return decorate(await self.render_core(base,user,origin),origin,base)

    async def transfer_source(self, credential_id, actor):
        credential = await self.credential(credential_id)
        if credential.user_id != actor.id:
            raise ValueError('Передать из этой карточки можно подписку, привязанную к вашему аккаунту.')
        return credential

    async def transfer_preview(self, credential_id, actor, identity):
        from app.services.accounts import display_freeman_id, freeman_id_aliases
        from app.services.admin_client_grant import rich
        from app.web.subscription_transfer import preview

        credential = await self.transfer_source(credential_id, actor)
        aliases = freeman_id_aliases(identity)
        if not aliases:
            raise ValueError('Введите Freeman ID клиента, например FO-123.')
        clients = list(await self.session.scalars(select(User).where(User.freeman_id.in_(aliases)).limit(2)))
        if len(clients) != 1:
            raise ValueError('Не удалось однозначно найти клиента по этому Freeman ID.')
        draft = await preview(self.session, self.settings, actor, credential.access_key_id, clients[0].id)
        if draft['source']['id'] != actor.id:
            raise ValueError('Владелец подписки уже изменился. Откройте карточку заново.')
        source, target = draft['source'], draft['target']
        def label(person):
            name = escape(person['name'])
            identity = escape(display_freeman_id(person['freeman_id']) or f'FO-{person["id"]}')
            return f'{name} · {identity}'
        name = escape(draft['key']['name'])
        from datetime import datetime
        expiry = expiry_date(datetime.fromisoformat(draft['key']['expires_at']))
        text = (f'🔁 <b>Передать подписку?</b>\n\n<b>Ключ:</b> {name}'
                f'\n<b>От кого:</b> {label(source)}\n<b>Кому:</b> {label(target)}'
                f'\n<b>Действует до:</b> {expiry}\n\n'
                'Клиент получит эту подписку в «Моих ключах». Ссылка, срок и привязки устройств сохранятся. '
                'Автопродление будет отключено.')
        # Keep the confirmation and the longest search origin under Telegram's 64-byte limit.
        token = urlsafe_b64encode(bytes.fromhex(draft['token'])).decode().rstrip('=')
        return rich(text, [
            [('✅ Передать подписку', f'admin:r:mv:{token}')],
            [('← Выбрать другого клиента', f'admin:r:input:transfer:{credential.id}')],
            [('← Ключ', f'admin:r:key:{credential.id}')],
        ])

    async def transfer_apply(self, token, actor):
        import re
        from app.database.models import AppSetting
        from app.services.admin_client_grant import rich
        from app.web.subscription_transfer import confirm

        if not re.fullmatch(r'[A-Za-z0-9_-]{22}', token):
            raise ValueError('Подтверждение недоступно.')
        token = urlsafe_b64decode(token+'==').hex()
        raw = await self.session.scalar(select(AppSetting.value).where(
            AppSetting.key == 'web_subscription_transfer:'+token))
        draft = json.loads(raw) if raw else {}
        if draft.get('actor') != actor.id or draft.get('source') != actor.id:
            raise PermissionError('Подтверждение передачи недоступно.')
        credential_id = await self.session.scalar(select(VlessCredential.id).where(
            VlessCredential.access_key_id == draft['key']))
        if not credential_id:
            raise ValueError('Ключ больше недоступен.')
        try:
            async with self.session.begin_nested():
                result=await confirm(self.session, self.settings, actor, token)
                from app.services.admin_client_notices import queue
                recipient=await self.session.get(User,result['target_id'])
                await queue(self.repo,recipient,actor,'transfer:'+token,'🔑 Вам передали VPN-подписку',
                            'Подписка уже доступна в разделе «Мои ключи».',
                            button=('Открыть подписку',f'sub:{result["subscription_id"]}'))
        except ValueError as error:
            return rich('⚠️ <b>Не удалось передать подписку</b>\n\n'+escape(str(error)),
                        [[('← Ключ', f'admin:r:key:{credential_id}')]])
        return rich('✅ <b>Подписка передана</b>\n\nКлиент найдёт её в «Моих ключах».',
                    [[('← Ключ', f'admin:r:key:{credential_id}')]])

    async def render_core(self, action, user,origin=''):
        self.service.admin(user)
        p = action.split(':')[2:]; page = p[0]
        if page=='search_results':
            data=json.loads(await self.repo.setting(f'remnawave_search:{user.telegram_id}','{}'));offset=max(0,int(p[1])) if len(p)>1 else 0
            if data.get('expires',0)<now().timestamp():return Screen('Поиск устарел.',[[('Найти ключ','admin:r:input:search')]])
            keys=list(await self.session.scalars(select(VlessCredential).where(VlessCredential.id.in_(data.get('ids',[])),VlessCredential.status!='deleted').order_by(VlessCredential.id.desc())))
            rows=[[(f'VLESS #{c.access_key_id} · клиент #{c.user_id}',f'admin:r:key:{c.id}')] for c in keys[offset:offset+5]]
            if offset:rows.append([('← Предыдущие',f'admin:r:search_results:{max(0,offset-5)}')])
            if offset+5<len(keys):rows.append([('Следующие →',f'admin:r:search_results:{offset+5}')])
            return Screen(f'Найдено: {len(keys)}',rows+[[('← Назад','admin:r:home')]])
        if page=='mv':
            return await self.transfer_apply(p[1],user)
        if page=='input' and p[1]=='transfer':
            credential=await self.transfer_source(p[2],user)
            return Screen('Введите Freeman ID получателя, например FO-123.',
                          [[('← Ключ',f'admin:r:key:{credential.id}')]],structured=True)
        if page == 'input' and p[1] != 'server':
            prompts = {'search':'Введите имя, Telegram ID, Freeman ID или название ключа.',
                'grant':'Введите: Freeman ID или Telegram ID | дни | устройства | название ключа\nНапример: FRM-000001 | 30 | 3 | Рабочий',
                'extend':'Введите число дней для продления (1–3650).',
                'limit':'Введите полный технический лимит с резервом (2–101). Например: 2 = 1 по тарифу + 1 резерв.',
                'traffic':'Введите лимит трафика в ГБ. 0 — без ограничений.',
                'name':'Введите название ключа (1–80 символов).',
                'panel':'Введите новое значение параметра панели.'}
            if p[1]=='grant':await self.repo.set_setting(f'vless_grant_intent:{user.id}:{p[2]}',secrets.token_hex(16))
            if p[1]=='extend':await self.repo.set_setting(f'vless_extend_intent:{user.id}:{p[2]}',secrets.token_hex(16))
            return Screen(prompts[p[1]], [[('← Отмена','admin:r:home')]])
        if page == 'keys':
            panel=int(p[1]);offset=max(0,int(p[2]));q=select(VlessCredential).where(VlessCredential.status!='deleted')
            if panel:q=q.where(VlessCredential.server_id==panel)
            keys=list(await self.session.scalars(q.order_by(VlessCredential.id.desc()).offset(offset).limit(6)))
            rows=[]
            for c in keys[:5]:
                key=await self.session.get(AccessKey,c.access_key_id);customer=await self.session.get(User,c.user_id)
                rows.append([(f'{"🟢" if c.status=="active" else "🟡"} {(key.name if key else "VLESS")[:35]} · {customer.freeman_id or customer.id}',f'admin:r:key:{c.id}')])
            nav=[]
            if offset:nav.append(('←',f'admin:r:keys:{panel}:{max(0,offset-5)}'))
            if len(keys)>5:nav.append(('→',f'admin:r:keys:{panel}:{offset+5}'))
            if nav:rows.append(nav)
            rows += [[('🔎 Поиск','admin:r:input:search')],[('← Назад',f'admin:r:server:{panel}' if panel else 'admin:r:home')]]
            return Screen('🔑 <b>VLESS-ключи</b>\nВыберите ключ для управления. По 5 записей на странице.',rows)
        if page=='edit':
            panel=await self.service.server(p[1]);fields={'name':'Название','country':'Страна / группа','internal_squad_uuid':'Internal Squad UUID','priority':'Приоритет','max_users':'Лимит пользователей','api_token':'API Token'}
            return Screen('⚙️ <b>Параметры Remnawave</b>',[[(label,f'admin:r:input:panel:{panel.id}:{field}')] for field,label in fields.items()]+[[('← Назад',f'admin:r:server:{panel.id}')]])
        if page in ('squads', 'squad'):
            panel=await self.service.server(p[1])
            try:data=await self.service.api(panel).squads()
            except RemnawaveApiError as e:raise ValueError(str(e)) from e
            data=data.get('response',data);items=data.get('internalSquads',data.get('items',[])) if isinstance(data,dict) else data
            if not isinstance(items,list):items=[]
            if page=='squad':
                chosen=next((row for row in items if str(row.get('uuid') or row.get('id'))==p[2]),None)
                if not chosen:raise ValueError('Группа больше не доступна в панели. Откройте список заново.')
                panel.internal_squad_uuid=p[2];await self.audit(user,'squad',panel.id)
                return await self.service.render(f'admin:r:server:{panel.id}',user)
            rows=[]
            offset=max(0,int(p[2])) if len(p)>2 else 0
            for row in items[offset:offset+5]:
                identity=str(row.get('uuid') or row.get('id') or '')
                target=f'admin:r:squad:{panel.id}:{identity}'
                if identity and len(target.encode())<=64:rows.append([(str(row.get('name') or identity)[:50],target)])
            if offset:rows.append([('← Предыдущие',f'admin:r:squads:{panel.id}:{max(0,offset-5)}')])
            if offset+5<len(items):rows.append([('Следующие →',f'admin:r:squads:{panel.id}:{offset+5}')])
            return Screen('👥 <b>Internal Squads</b>\nВыберите группу подключений для выдачи VLESS.' if items else 'В панели нет Internal Squads. Создайте группу в Remnawave и откройте этот список снова.',rows+[[('Указать UUID вручную',f'admin:r:input:panel:{panel.id}:internal_squad_uuid')],[('← Назад',f'admin:r:server:{panel.id}')]])
        if page in ('devices','device','device_confirm','device_apply'):
            from app.services.admin_devices import render
            return await render(self,p,user,origin)
        if page=='client':
            from app.services.admin import AdminService
            c=await self.credential(p[1])
            screen=await AdminService(self.repo,self.settings).render(f'admin:client:{c.user_id}',user)
            screen.rows=[r for r in screen.rows if not any(l.startswith('←') for l,_ in r)]+[[('← Ключ',f'admin:r:key:{c.id}')]]
            return screen
        if page in ('key','refresh','qr'):
            c=await self.credential(p[1]);key=await self.session.get(AccessKey,c.access_key_id);customer=await self.session.get(User,c.user_id);panel=await self.service.server(c.server_id)
            rows=[[('📲 Открыть подписку','url:'+c.subscription_url)],
                [('📱 Устройства',f'admin:r:devices:{c.id}')],
                [('🟢 Продлить',f'admin:renewcustom:devices:{c.subscription_id}'),('✏️ Имя',f'admin:r:input:name:{c.id}')],
                [('Лимит устройств',f'admin:r:input:limit:{c.id}')],
                [('Разблокировать' if c.status!='active' else 'Заблокировать',f'admin:r:confirm:{"unblock" if c.status!="active" else "block"}:{c.id}')],
                [('Перевыпустить',f'admin:r:confirm:rotate:{c.id}')],
                [('♻️ Отвязать все устройства',f'admin:r:confirm:devices:{c.id}')],
                [('💰 Изменить цену продажи',f'admin:saleprice:list:{c.id}:0')],
                [('🗑 Удалить ключ',f'admin:r:confirm:delete:{c.id}')],[('👤 Клиент',f'admin:r:client:{c.id}')],[('← Назад',f'admin:r:keys:{c.server_id}:0')]]
            from app.services.key_device_counts import device_counts
            try:count=(await device_counts([(c.id,self.service.api(panel),c.remote_user_uuid)]))[c.id]
            except Exception:count=None
            sub=await self.session.get(Subscription,c.subscription_id)
            if sub and key and c.user_id==user.id and c.status=='active' and key.status=='active' and sub.expires_at.replace(tzinfo=timezone.utc)>now():
                rows.insert(-2,[('🔁 Передать подписку',f'admin:r:input:transfer:{c.id}')])
            addon_label='Не подключена'
            if sub:
                from app.services.bot_antig_addon import has_access, deadline
                addon_enabled=await has_access(self.repo,sub)
                if addon_enabled:
                    addon_label=f'До {expiry_date(await deadline(self.repo,sub))}'
                if sub.expires_at.replace(tzinfo=timezone.utc)>now() and c.status=='active' and key and key.status=='active':
                    label='🛡 Отключить антиглушилку' if addon_enabled else '🛡 Подключить антиглушилку'
                    rows.insert(3,[(label,f'admin:renewcustom:addon:{sub.id}')])
                    if not sub.tariff_id and not addon_enabled:
                        rows.insert(4,[('Цена антиглушилки для клиента',f'admin:input:antig_custom:{sub.id}:{c.id}')])
            tariff=await self.repo.tariff_by_id(sub.tariff_id) if sub and sub.tariff_id else None
            from app.services.screens import duration
            from app.services.bot_catalog import devices_label
            from math import ceil
            term=duration(tariff.months) if tariff else ('Пробная подписка' if sub and sub.kind=='trial' else f'{max(1,ceil((c.expires_at-c.created_at).total_seconds()/86400))} дн.')
            if sub and sub.kind=='admin' and not tariff:
                term=await self.repo.setting(f'subscription_duration:{sub.id}',term)
            name=await self.repo.setting(f'subscription_name:{c.subscription_id}','') or (key.name if key else 'Подписка')
            if sub:
                from app.services.subscription_labels import current
                name,term=await current(self.repo,sub,key)
            import re
            if re.match(r'^(?:vless(?:\s|·|$)|https?://)',name,re.I):name='Подписка'
            bound=f'{count} из {technical(c.device_limit)}' if count is not None else 'Не удалось проверить'
            remaining={'deleted':'Ключ удалён','disabled':'Доступ приостановлен','blocked':'Доступ приостановлен','frozen':'Доступ приостановлен'}.get(c.status,time_remaining(c.expires_at))
            from app.services.accounts import display_freeman_id
            identity=escape(display_freeman_id(customer.freeman_id) or f'FO-{customer.id}') if customer else '—'
            customer_label=f'{escape(customer.display_name)} · {identity}' if customer else identity
            text=f'🔑 <b>{escape(name)}</b>\n\n<b>Клиент:</b> {customer_label}\n<b>Подписка:</b> {escape(term)} · {devices_label(c.device_limit)} (тариф)\n<b>Лимит:</b> {technical(c.device_limit)} · резерв: 1\n\n<b>Подключено устройств:</b> {bound}\n\n<b>Осталось:</b> {remaining}\n\n<b>Действует до:</b> {expiry_date(sub.expires_at if sub else c.expires_at)}\n<b>Антиглушилка:</b> {addon_label}\n\n<b>Ключ</b>\n<code>{escape(c.subscription_url)}</code>'
            from app.services.qr_image import png
            image=await png(c.subscription_url)
            return Screen(text,rows,photo=(image,f'vless-{c.id}.png'),structured=True)
        if page=='confirm':
            operation=p[1];c=await self.credential(p[2]);allowed={'block','unblock','rotate','delete','devices','traffic'}
            if operation not in allowed:raise ValueError('Действие недоступно.')
            token=secrets.token_hex(10)
            await self.repo.set_setting('vless_confirm:'+token,json.dumps(dict(actor=user.telegram_id,cid=c.id,operation=operation,expires=now().timestamp()+300,status=c.status)))
            descriptions={'delete':'Удалить ключ безвозвратно?','rotate':'Перевыпустить ключ? Старая ссылка перестанет работать.','devices':'♻️ <b>Отвязать все устройства?</b>\n\nВсе HWID-привязки этого ключа будут сняты после подтверждения панелью. Лимит, срок, трафик и ссылка сохранятся. Затем клиент сможет добавить нужное устройство заново.\n\nЭто снимает ограничение занятых мест, но не отменяет ручную блокировку, окончание срока или лимит трафика. Если ключ отдельно заблокирован, используйте «Включить доступ» после проверки причины.','traffic':'Сбросить счётчик трафика?','block':'Заблокировать доступ?','unblock':'Включить доступ?'}
            return Screen(descriptions[operation],[[('Подтвердить',f'admin:r:apply:{token}')],[('← Отмена',f'admin:r:key:{c.id}')]],structured=True)
        if page=='apply':
            import re
            from app.database.models import AppSetting
            if not re.fullmatch(r'[0-9a-f]{20}',p[1]):raise ValueError('Подтверждение недоступно.')
            row=await self.session.scalar(select(AppSetting).where(AppSetting.key=='vless_confirm:'+p[1]).with_for_update().execution_options(populate_existing=True))
            data=json.loads(row.value) if row else {}
            if data.get('actor')!=user.telegram_id or data.get('expires',0)<now().timestamp() or data.get('done'):raise ValueError('Подтверждение истекло или использовано.')
            c=await self.credential(data['cid']);operation=data['operation']
            if c.status!=data['status']:raise ValueError('Ключ изменился. Подтвердите действие заново.')
            panel=await self.service.server(c.server_id)
            if operation=='unblock':
                customer=await self.session.get(User,c.user_id)
                sub=await self.session.get(Subscription,c.subscription_id)
                if not customer or customer.status!='active' or not sub or sub.expires_at.replace(tzinfo=timezone.utc)<=now():
                    raise ValueError('Разблокировка недоступна: проверьте статус клиента и срок подписки.')
                if c.traffic_limit>0 and c.traffic_used>=c.traffic_limit:
                    raise ValueError('Лимит трафика исчерпан. Сначала проверьте условия тарифа; сброс устройств не сбрасывает трафик.')
            try:
                if operation=='devices':
                    await self.service.api(panel).delete_all_devices(c.remote_user_uuid)
                    from app.services.vless_hwid import confirmed_removal
                    await confirmed_removal(self.repo,c)
                elif operation=='traffic':await self.service.api(panel).reset_traffic(c.remote_user_uuid);c.traffic_used=0
                else:await vless_provision.control(self.repo,self.settings,c,operation)
            except RemnawaveApiError as e:raise ValueError(str(e)) from e
            key=await self.session.get(AccessKey,c.access_key_id)
            if operation in ('delete','block','unblock'):
                c.status={'delete':'deleted','block':'disabled','unblock':'active'}[operation]
                if key:key.status={'delete':'deleted','block':'blocked','unblock':'active'}[operation]
                await self.repo.set_setting(f'subscription_status:{c.subscription_id}',c.status)
                if operation=='delete':await self.repo.set_setting(f'subscription_removed:{c.subscription_id}','1');await self.repo.set_setting(f'auto_renew:{c.subscription_id}','0')
            data['done']=True;await self.repo.set_setting('vless_confirm:'+p[1],json.dumps(data));await self.audit(user,operation,c.id)
            screen=await self.render(f'admin:r:keys:{c.server_id}:0' if operation=='delete' else f'admin:r:key:{c.id}',user)
            if operation=='devices':
                screen.text='✅ <b>Все привязки сняты</b>\n\nКлиент может заново добавить устройство. Срок и лимит подписки сохранены.\n\n'+screen.text
                if screen.rich:
                    from aiogram.types import InputRichMessage
                    from app.bot.rich_copy import text_blocks
                    screen.rich=InputRichMessage(blocks=text_blocks(screen.text),skip_entity_detection=True)
            return screen
        return None

    async def input(self, action, user, text):
        from app.services.admin_key_navigation import split,decorate
        base,origin=split(action)
        return decorate(await self.input_core(base,user,text),origin,base)

    async def input_core(self, action, user, text):
        self.service.admin(user);p=action.split(':')[3:];operation=p[0];text=text.strip()
        if operation=='transfer':
            return await self.transfer_preview(p[1],user,text)
        if operation=='search':
            needle='%'+text.replace('%','\\%').replace('_','\\_')+'%'
            q=select(VlessCredential).join(User,User.id==VlessCredential.user_id).join(AccessKey,AccessKey.id==VlessCredential.access_key_id).where(VlessCredential.status!='deleted',or_(User.first_name.ilike(needle,escape='\\'),User.username.ilike(needle,escape='\\'),User.freeman_id.ilike(needle,escape='\\'),AccessKey.name.ilike(needle,escape='\\'),User.telegram_id==int(text) if text.isdigit() else False)).limit(20)
            keys=list(await self.session.scalars(q))
            await self.repo.set_setting(f'remnawave_search:{user.telegram_id}',json.dumps(dict(ids=[c.id for c in keys],expires=now().timestamp()+1800)))
            return await self.render('admin:r:search_results:0',user)
        if operation=='grant':
            fields=[t.strip() for t in text.split('|')]
            if len(fields) not in (3,4):raise ValueError('Формат: Freeman ID | дни | устройства | название')
            identity,days,limit=fields[:3]
            if not days.isdigit() or not 1<=int(days)<=3650 or not limit.isdigit() or not 1<=int(limit)<=100:raise ValueError('Дни: 1–3650. Устройства: 1–100.')
            customer=await self.session.scalar(select(User).where(User.freeman_id==identity.upper(),User.status!='blocked')) if not identity.isdigit() else await self.session.scalar(select(User).where(User.telegram_id==int(identity),User.status!='blocked'))
            if not customer:raise ValueError('Клиент не найден. Используйте Freeman ID или Telegram ID клиента из базы.')
            name=fields[3] if len(fields)==4 else f'{now():%d.%m.%Y} · {days} дней'
            if not 1<=len(name)<=80:raise ValueError('Название: 1–80 символов.')
            panel=await self.service.server(p[1]);sub=await self.repo.add(Subscription(user_id=customer.id,kind='admin',expires_at=now()+timedelta(days=int(days)),device_limit=int(limit)))
            token=await self.repo.setting(f'vless_grant_intent:{user.id}:{panel.id}')
            if not token:raise ValueError('Откройте выдачу ключа заново для подтверждения заказа.')
            c=await vless_provision.provision(self.repo,self.settings,sub,customer,panel,user,operation_key='bot-grant:'+token);key=await self.session.get(AccessKey,c.access_key_id);key.name=name
            await self.audit(user,'grant',c.id);return await self.render(f'admin:r:key:{c.id}',user)
        if operation=='panel':
            panel=await self.service.server(p[1]);field=p[2]
            if field in ('priority','max_users'):
                maximum=999 if field=='priority' else 1000000
                if not text.isdigit() or not 1<=int(text)<=maximum:raise ValueError(f'Введите число от 1 до {maximum}.')
                value=int(text)
            elif field=='api_token':
                if not 20<=len(text)<=2000:raise ValueError('Неверная длина API Token.')
                from app.services.remnawave import RemnawaveApiClient
                bearer,gateway=self.service.credentials(panel)
                try:await RemnawaveApiClient(panel.api_url,text,gateway).health()
                except RemnawaveApiError as e:raise ValueError(str(e)) from e
                value=self.service.save_credentials(text,gateway)
            elif field in ('name','country','internal_squad_uuid'):
                if not 1<=len(text)<=80:raise ValueError('Введите 1–80 символов.')
                value=text
            else:raise ValueError('Параметр недоступен.')
            setattr(panel,field,value);await self.audit(user,'panel_'+field,panel.id)
            return await self.service.render(f'admin:r:server:{panel.id}',user)
        c=await self.credential(p[1]);panel=await self.service.server(c.server_id);key=await self.session.get(AccessKey,c.access_key_id);sub=await self.session.get(Subscription,c.subscription_id)
        if operation=='extend':
            from app.services.bot_admin_renew import BotAdminRenew
            return await BotAdminRenew(self.repo,self.settings).input(sub.id,c.device_limit,text,user)
        if operation=='name':
            if not 1<=len(text)<=80:raise ValueError('Название: 1–80 символов.')
            key.name=text;await self.repo.set_setting(f'subscription_name:{sub.id}',text)
        else:
            maximum=3650 if operation=='extend' else 101 if operation=='limit' else 100000
            if operation not in ('extend','limit','traffic') or not text.isdigit() or not (0 if operation=='traffic' else 1)<=int(text)<=maximum:raise ValueError('Введите допустимое целое число.')
            identity=c.remote_user_uuid;payload={('id' if identity.isdigit() else 'uuid'):int(identity) if identity.isdigit() else identity}
            value=int(text)
            if operation=='limit':
                if value<2:raise ValueError('Полный лимит с резервом: от 2 до 101.')
                value-=1
                payload['hwidDeviceLimit']=value
            if operation=='traffic':payload.update(trafficLimitBytes=value*1024**3,trafficLimitStrategy='NO_RESET')
            try:await self.service.api(panel).update_user(payload)
            except RemnawaveApiError as e:raise ValueError(str(e)) from e
            if operation=='limit':
                c.device_limit=sub.device_limit=value
                from app.services.subscription_labels import refresh
                await refresh(self.repo,sub,key)
            if operation=='traffic':c.traffic_limit=value*1024**3
            c.last_sync=now()
        await self.audit(user,operation,c.id);return await self.render(f'admin:r:key:{c.id}',user)
