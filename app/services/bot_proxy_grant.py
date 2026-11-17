"""Owner-only Telegram issuance UI, using existing Telemt and invite services."""
import json
import re
import secrets
from datetime import timedelta
from html import escape
from urllib.parse import urlencode

from sqlalchemy import select, or_
from app.database.models import AppSetting, ProxyCredential, User, AdminAudit, now
from app.services.admin_client_grant import ClientGrant, rich
from app.services.bot_grant_config import parse_combined


class BotProxyGrant:
    def __init__(self, repo, settings):
        self.repo, self.settings = repo, settings

    def require(self, actor):
        if not self.settings.is_owner(actor.telegram_id):
            raise PermissionError('Выдача прокси доступна только администратору.')

    async def customer(self, uid, actor):
        return await ClientGrant(self.repo, self.settings).customer(uid, actor)

    async def home(self, actor):
        self.require(actor)
        screen=rich('🌐 <b>Telegram Proxy · Выдача</b>\n\n'
            'Выдайте отдельный прокси себе, существующему клиенту или создайте приглашение для нового клиента.'
            '\n\nСрок и число подключений задаются одним сообщением. Выдача бесплатная; подарочный прокси вместе с VPN сохраняется отдельно.'
            '\n\n🔗 Подарок по ссылке получает один человек. Для публикации используйте рекламную ссылку.', [
                [('🎁 Выдать или подарить прокси', 'admin:proxygrant:choose')],
                [('📣 Рекламные ссылки', 'admin:proxycampaign')],
                [('📨 Подарочные прокси', 'admin:giftlink:list:proxy:0')],
                [('🌐 Мои прокси', 'admin:proxygrant:mine:0')],
                [('⚙️ Серверы и настройки', 'admin:p:home')],
                [('← Главное меню', 'home')],
            ])

        target='admin:proxygrant:choose'
        screen.button_options[target]={'style':'success'}
        icon=await self.repo.setting('button_icon:gift_link_proxy')
        if icon.isdigit():
            from app.config.admin_content import without_leading_emoji
            screen.button_options[target]['icon_custom_emoji_id']=icon
            screen.rows=[[(without_leading_emoji(label) if action==target else label,action) for label,action in row] for row in screen.rows]
        return screen

    async def save(self, token, data):
        await self.repo.set_setting('bot_proxy_grant:'+token, json.dumps(data))

    async def draft(self, token, actor):
        self.require(actor)
        if not re.fullmatch(r'[0-9a-f]{16}', token):
            raise ValueError('Выдача недоступна. Начните заново.')
        row = await self.repo.session.scalar(select(AppSetting).where(AppSetting.key == 'bot_proxy_grant:'+token)
            .with_for_update().execution_options(populate_existing=True))
        data = json.loads(row.value) if row else {}
        if data.get('actor') != actor.telegram_id:
            raise PermissionError('Выдача другого администратора.')
        if data.get('credential') or data.get('invitation'):
            return data
        if data.get('expires', 0) < now().timestamp():
            raise ValueError('Настройка устарела. Начните новую выдачу.')
        if data.get('user'):
            await self.customer(data['user'], actor)
        return data

    async def prompt(self, token, field, actor):
        data = await self.draft(token, actor)
        if data.get('credential') or data.get('invitation'):
            raise ValueError('Уже выдано. Для нового прокси начните новую выдачу.')
        if field == 'recipient':
            text = ('👤 <b>Кому выдать прокси?</b>\n\nОтправьте Freeman ID (например, <code>FO-150</code>), '
                'Telegram ID с префиксом <code>tg:123456789</code>, @username или имя клиента.'
                '\n\nЕсли клиент ещё не зарегистрирован, вернитесь и создайте приглашение.')
        elif field == 'params':
            if data['mode'] != 'link' and not data.get('user'):
                return await self.prompt(token, 'recipient', actor)
            recipient = 'Первый получатель приглашения' if data['mode'] == 'link' else ClientGrant(self.repo, self.settings).identity(await self.customer(data['user'], actor))
            text = ('🌐 <b>Параметры Telegram Proxy</b>\n\nПолучатель: '+recipient+
                '\n\nОтправьте число подключений и срок в днях через запятую:'
                '\n\n<code>2 подключения, 60 дней</code>\nили <code>2, 60</code>'
                '\n\nЛимит подключений считается по уникальным IP, а не по моделям телефонов. '
                'TCP-лимит будет не ниже серверного значения, чтобы Telegram работал стабильно.')
        else:
            raise ValueError('Настройка недоступна.')
        screen = rich(text, [[('← Назад', 'p:home')]])
        screen.admin_input_action = f'admin:input:proxygrant:{token}:{field}'
        return screen

    async def input(self, token, field, value, actor):
        data = await self.draft(token, actor)
        if data.get('credential') or data.get('invitation'):
            raise ValueError('Прокси уже выдан. Начните новую выдачу.')
        if field == 'recipient':
            value = value.strip()
            if not 1 <= len(value) <= 100:
                raise ValueError('Укажите имя, Freeman ID или Telegram ID клиента.')
            from app.services.accounts import find_freeman_id
            if re.fullmatch(r'(?:FO-|FRM-|Freeman\s+)\s*\d+', value, re.I) or value.isdigit():
                item = await find_freeman_id(self.repo.session, value)
                candidates = [item] if item else []
            elif re.fullmatch(r'tg:\d{1,19}', value, re.I):
                number = int(value[3:])
                if number >= 2**63:
                    raise ValueError('Проверьте Telegram ID.')
                candidates = list(await self.repo.session.scalars(select(User).where(User.telegram_id == number)))
            else:
                term = value.lstrip('@').replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                candidates = list(await self.repo.session.scalars(select(User).where(User.status == 'active',
                    or_(User.username.ilike('%'+term+'%', escape='\\'), User.first_name.ilike('%'+term+'%', escape='\\')))
                    .order_by(User.id.desc()).limit(11)))
            candidates = [u for u in candidates if u.status == 'active' and await self.repo.setting(f'user_archived:{u.id}') != '1']
            if not candidates:
                raise ValueError('Клиент не найден. Уточните Freeman ID или создайте приглашение для нового клиента.')
            rows = [[(u.display_name[:35]+' · '+(u.freeman_id or f'FO-{u.id}'),
                      f'admin:proxygrant:select:{token}:{u.id}')] for u in candidates[:10]]
            rows += [[('🔎 Другой поиск', f'admin:input:proxygrant:{token}:recipient')], [('← Назад', 'p:home')]]
            return rich('👤 <b>Выберите получателя</b>\n\n'+('Показаны первые 10 совпадений. При необходимости уточните поиск.' if len(candidates)>10 else 'Прокси будет привязан только к выбранному клиенту.'), rows)
        if field != 'params':
            raise ValueError('Настройка недоступна.')
        changes = parse_combined(value.strip(), proxy=True)
        if data['mode'] != 'link':
            await self.customer(data['user'], actor)
        data.update(changes)
        data['revision'] = secrets.token_hex(4)
        data.pop('end', None)
        await self.save(token, data)
        return await self.confirmation(token, data, actor)

    async def confirmation(self, token, data, actor):
        recipient = 'Первый получатель приглашения' if data['mode'] == 'link' else ClientGrant(self.repo, self.settings).identity(await self.customer(data['user'], actor))
        link = data['mode'] == 'link'
        screen = rich('🌐 <b>Вы выбрали Telegram Proxy</b>\n\nПолучатель: '+recipient+
            f'\n\n<b>Подключений (уникальных IP):</b> {data["devices"]}\n<b>Срок:</b> {data["amount"]} дн.'+
            ('\n\nСсылка действует 3 дня и выдаёт прокси одному аккаунту. Срок прокси начнётся при получении. '
             'Не публикуйте приглашение: получить его сможет первый открывший.' if link else
             '\n\nСрок начнётся после выдачи. Без списания с баланса и без платежа.')+
            '\n\nДоступ появится только после подтверждения сервером.', [
                [('🔗 Создать ссылку' if link else '✅ Выдать прокси', f'admin:proxygrant:apply:{token}:{data["revision"]}')],
                [('← Изменить', f'admin:input:proxygrant:{token}:params')],
            ])
        screen.button_options[f'admin:proxygrant:apply:{token}:{data["revision"]}'] = {'style': 'success'}
        return screen

    async def apply(self, token, actor, revision=None):
        # Roll back local partial work on provider errors. Stable operation identity
        # recovers a remotely created proxy after an uncertain network response.
        data = await self.draft(token, actor)
        if revision is not None and revision != data.get('revision'):
            raise ValueError('Параметры изменились. Используйте последнюю карточку выдачи.')
        if data.get('credential') or data.get('invitation'):
            return await self.result(data, actor)
        if not data.get('amount') or not data.get('devices'):
            raise ValueError('Сначала укажите подключения и срок.')
        from app.services.proxies import ProxyService
        service = ProxyService(self.repo, self.settings)
        if not data.get('server'):
            servers = await service.matching_servers('paid')
            if not servers:
                raise ValueError('Нет доступного проверенного сервера для отдельного прокси. Проверьте серверы и повторите выдачу.')
            data['server'] = servers[0].id
        if not data.get('end'):
            data['end'] = (now()+timedelta(days=data['amount'])).isoformat()
            await self.save(token, data)
        async with self.repo.session.begin_nested():
            data = await self.draft(token, actor)
            if data.get('credential') or data.get('invitation'):
                return await self.result(data, actor)
            if not data.get('amount') or not data.get('devices'):
                raise ValueError('Сначала укажите подключения и срок.')
            server = await service.server(data['server'])
            tcp = max(data['devices'], server.tcp_connection_limit)
            if data['mode'] == 'link':
                from app.services.proxy_invites import ProxyInvites
                data['invitation'] = await ProxyInvites(self.repo, self.settings).create(actor, server.id, data['amount'], data['devices'], tcp)
            else:
                user = await self.customer(data['user'], actor)
                from datetime import datetime
                expiry = datetime.fromisoformat(data['end'])
                credential, _ = await service.provision(server, user, expiry, data['devices'], tcp,
                    force_kind='paid', connection_token='botgrant_'+token)
                if not credential or credential.user_id != user.id or not credential.link:
                    raise ValueError('Сервер не подтвердил прокси. Повторите выдачу.')
                from app.services.proxy_control import meta
                details = await meta(self.repo, credential)
                details.source = 'admin'
                details.ip_limit = data['devices']
                data['credential'] = credential.id
                await self.repo.add(AdminAudit(actor_telegram_id=actor.telegram_id, action='bot_proxy_grant',
                    target=str(user.id), details=f'credential={credential.id}; days={data["amount"]}; ip={data["devices"]}'))
                from app.services.admin_client_notices import queue
                await queue(self.repo,user,actor,f'proxy:{credential.id}','🎁 Вам выдали Telegram Proxy',
                            f'Доступ на {data["amount"]} дн. · до {data["devices"]} уникальных IP.',
                            proxy=credential.id)
            await self.save(token, data)
            return await self.result(data, actor)

    async def result(self, data, actor):
        self.require(actor)
        if data.get('invitation'):
            link = data['invitation']['link']
            message = ('🎁 Вам выдали Telegram Proxy!\n\nПерейдите в бот, чтобы получить свой прокси и подключить Telegram без VPN.\n\n'+
                f'Срок: {data["amount"]} дн. · До {data["devices"]} подключений (уникальных IP).\n\n'+link)
            share = 'url:https://t.me/share/url?'+urlencode({'url': link, 'text': message.rsplit('\n\n', 1)[0]})
            return rich('🔗 <b>Приглашение готово</b>\n\nОтправьте клиенту эту ссылку:\n\n<code>'+escape(link)+
                '</code>\n\nПервый получатель откроет бота, зарегистрируется и получит кнопку подключения. '
                'Приглашение одноразовое, действует 3 дня. Срок прокси начинается при получении.', [
                    [('↗️ Поделиться приглашением', share)], [('← Telegram Proxy', 'p:home')]])
        from app.services.proxies import ProxyService
        service = ProxyService(self.repo, self.settings)
        credential = await self.repo.session.get(ProxyCredential, data['credential'])
        user = await self.customer(data['user'], actor)
        if not credential or credential.user_id != user.id:
            raise ValueError('Выданный прокси недоступен.')
        from app.services.offers import utc
        live = credential.status == 'active' and utc(credential.expires_at) > now()
        rows = []
        options = {}
        if live and credential.link:
            target = 'url:'+service.public_link(service.decrypt(credential.link))
            rows.append([('✈️ Подключить прокси', target)])
            options[target] = {'style': 'primary'}
        rows.append([('← Telegram Proxy', 'p:home')])
        screen = rich(('✅ <b>Прокси выдан</b>' if live else '⌛ <b>Доступ завершён</b>')+
            '\n\nКлиент: '+ClientGrant(self.repo, self.settings).identity(user)+
            f'\n\nСрок до: {credential.expires_at:%d.%m.%Y %H:%M} UTC\nПодключений (уникальных IP): {data["devices"]}'+
            '\n\nДоступ сохранён в разделе Telegram Proxy этого клиента.', rows)
        screen.button_options = options
        return screen

    async def render(self, action, actor):
        self.require(actor)
        parts = action.split(':')
        operation = parts[2]
        if operation == 'choose':
            return rich('🎁 <b>Кому выдать Telegram Proxy?</b>\n\nСебе — доступ вашему аккаунту.\n\nКлиенту — существующему клиенту из базы.\n\nПо ссылке — первому получателю, даже если он ещё не зарегистрирован.',[[('👤 Себе','admin:proxygrant:new:self')],[('👥 Клиенту из базы','admin:proxygrant:new:customer')],[('🎁 Подарить по ссылке','admin:input:giftlink:proxy')],[('← Telegram Proxy','p:home')]])
        if operation == 'new':
            mode = parts[3]
            if mode not in ('self', 'customer', 'link'):
                raise ValueError('Выберите способ выдачи.')
            token = secrets.token_hex(8)
            data = dict(actor=actor.telegram_id, mode=mode, expires=now().timestamp()+1800)
            if mode == 'self':
                await self.customer(actor.id, actor)
                data['user'] = actor.id
            await self.save(token, data)
            return await self.prompt(token, 'recipient' if mode == 'customer' else 'params', actor)
        if operation == 'mine':
            from app.services.proxies import ProxyService
            return await ProxyService(self.repo, self.settings).customer_screen(actor, max(0, int(parts[3])))
        token = parts[3]
        if operation == 'apply':
            if len(parts) != 5:
                raise ValueError('Подтверждение устарело. Укажите параметры заново.')
            return await self.apply(token, actor, parts[4])
        if operation == 'select':
            data = await self.draft(token, actor)
            if data['mode'] != 'customer' or data.get('credential') or data.get('invitation') or data.get('server'):
                raise ValueError('Получателя изменить нельзя. Начните новую выдачу.')
            user = await self.customer(int(parts[4]), actor)
            data['user'] = user.id
            for key in ('amount', 'devices', 'revision', 'server', 'end'):
                data.pop(key, None)
            await self.save(token, data)
            return await self.prompt(token, 'params', actor)
        raise ValueError('Действие недоступно.')
