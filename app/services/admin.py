from datetime import timedelta, timezone

from decimal import Decimal, InvalidOperation

import json, secrets, re

import re as regex

from html import escape

from urllib.parse import urlparse

from sqlalchemy import select, func, or_, delete as sql_delete

from app.database.models import User, Subscription, Tariff, Payment, AccessKey, Server, AdminAudit, PromoCode, BalanceTransaction, AppSetting, now

from app.config.admin_content import (EDITABLE_BUTTONS, BUTTON_GROUPS, button_group, SETTING_FIELDS,

    CUSTOM_BUTTON_LOCATIONS, CUSTOM_BUTTON_TARGETS)

from app.bot.keyboards import back

from app.services.screens import Screen, money, duration

from app.services.accounts import display_freeman_id





class AdminService:

    def __init__(self, repo, settings):

        self.repo, self.settings = repo, settings

        self.session = repo.session



    def require(self, actor):

        if not self.settings.is_admin(actor.telegram_id):

            raise PermissionError('Admin access denied')



    async def audit(self, actor, action, target, details):

        event=await self.repo.add(AdminAudit(actor_telegram_id=actor.telegram_id, action=action, target=str(target), details=str(details)))
        from app.services.admin_client_notices import queue
        if action=='balance_credit':
            user=await self.session.get(User,int(target))
            amount=int(str(details).split(' ',1)[0])
            await queue(self.repo,user,actor,event.id,'💰 Баланс пополнен',
                        f'Зачислено {money(amount)}. Текущий баланс {money(await self.repo.balance(user.id))}.',
                        button=('Открыть баланс','balance'))
        elif action in ('client_discount','client_price','referral_personal'):
            user=await self.session.get(User,int(target))
            title='Обновлены условия реферальной программы' if action=='referral_personal' else 'Обновлены ваши персональные цены'
            button=('Реферальная программа','referral') if action=='referral_personal' else ('Посмотреть тарифы','buy')
            await queue(self.repo,user,actor,event.id,title,button=button)
        elif action=='block':
            user=await self.session.get(User,int(target))
            title='Доступ к аккаунту восстановлен' if details=='active' else 'Доступ к аккаунту приостановлен'
            await queue(self.repo,user,actor,event.id,title)
        elif action in ('extend','keydelete'):
            item=await self.session.get(Subscription if action=='extend' else AccessKey,int(target))
            if item:
                user=await self.session.get(User,item.user_id)
                title='🔄 Подписка продлена' if action=='extend' else '🔑 Ключ удалён'
                await queue(self.repo,user,actor,event.id,title,button=('Мои ключи','subscriptions'))



    async def customer(self, id):

        customer = await self.session.get(User, int(id))

        if not customer or await self.repo.setting(f'user_archived:{customer.id}') == '1':

            raise ValueError('Клиент не найден.')

        return customer



    async def archived_users(self):

        values = await self.repo.settings_prefix('user_archived:')

        return {int(key.split(':', 1)[1]) for key, value in values.items()

                if value == '1' and key.split(':', 1)[1].isdigit()}



    async def label(self, tariff):

        return await self.repo.tariff_setting(tariff, 'tariff_name', duration(tariff.months))



    async def custom_button(self, token):

        if not re.fullmatch(r'[0-9a-f]{8}', token or ''):

            raise ValueError('Кнопка не найдена.')

        try: item=json.loads(await self.repo.setting('custom_button:'+token))

        except (json.JSONDecodeError,TypeError): item={}

        if not item: raise ValueError('Кнопка не найдена.')

        return item



    @staticmethod

    def validate_custom_target(value):

        if value in CUSTOM_BUTTON_TARGETS: return value

        parsed=urlparse(value)

        if parsed.scheme=='https' and parsed.hostname and not parsed.username and not parsed.password:

            return value

        raise ValueError('Укажите разрешённый раздел бота или публичную HTTPS-ссылку.')



    async def assignment_preview(self, key_id, customer_id, actor):

        from app.services.keys import KeyService

        key, customer = await KeyService(self.repo, self.settings).preview_assignment(actor, key_id, customer_id)

        if key.user_id == customer.id:

            return Screen('Этот ключ уже назначен клиенту.', [back(f'admin:keys:{customer.id}')])

        owner = await self.customer(key.user_id)

        import json, secrets

        token = secrets.token_hex(6)

        await self.repo.set_setting('confirm:' + token, json.dumps({'actor': actor.telegram_id, 'operation': 'keyassign',

            'key_id': key.id, 'customer_id': customer.id, 'expected_owner': key.user_id,

            'expected_status': key.status, 'expires': now().timestamp() + 300}))

        text = f'🔑 <b>Назначить ключ</b>\n\nКлюч: #{key.id} · {escape(key.name)}\nСтатус: {key.status}\nДо: {key.expires_at:%d.%m.%Y %H:%M} UTC\nТекущий владелец: {owner.telegram_id} · {escape(owner.display_name)}\nНовый владелец: {customer.telegram_id} · {escape(customer.display_name)}\n\nКлюч будет перенесён, а у прежнего клиента исчезнет из списка. Срок и статус сохранятся.\nРежим тестовый: настоящая конфигурация не передаётся.'

        return Screen(text, [[('✅ Назначить клиенту', f'admin:keyapply:{key.id}:{customer.id}:{token}')], back(f'admin:issue:{customer.id}')])



    async def render(self, action, actor):

        from app.services.admin_key_navigation import split,route,search_context

        action,origin=split(action)

        self.require(actor)

        if not self.settings.can_admin_action(actor.telegram_id,action):

            raise PermissionError('Недостаточно прав для этого раздела.')

        if action.startswith('admin:guide:'):

            from app.services.guides import guide

            return guide(action.split(':')[2])

        screen = await self._render(route(action,origin) if action.startswith('admin:r:') else action, actor, origin)

        if action.startswith('admin:keys:'):

            client_origin='c'+action.split(':')[2]+('.'+origin if origin=='h' or origin.startswith('s') else '')

            for row in screen.rows:

                for i,(label,target) in enumerate(row):

                    if target.startswith('admin:key:'):row[i]=(label,route(target,client_origin))

        if action.startswith(('admin:giftlink:','admin:input:giftlink:')):
            from app.services.screens import ScreenService
            screen=await ScreenService(self.repo,self.settings,None,None).decorate('gift_links',actor,screen)
            if action.startswith('admin:giftlink:post:'):
                screen.rows=[row for row in screen.rows if any(target.startswith('url:') and '?start=fg_' in target for _,target in row)]
            return screen
        if screen.admin_input_action and origin:

            screen.admin_input_action += '~'+origin

        if action=='admin:protocol:vless' or action.startswith(('admin:topups:','admin:hidden')):return screen

        if not any(label.startswith('←') for row in screen.rows for label,_ in row):

            screen.rows.append(back('home' if action=='admin:home' else 'admin:home'))

        if action.startswith(('admin:renewcustom:','admin:input:renewcustom:','admin:input:agfee:','admin:input:antig_custom:','admin:saleprice:','admin:input:saleprice:')):
            from app.services.admin_key_navigation import decorate
            return decorate(screen,origin,action)
        return search_context(screen,origin,action)



    async def _render(self, action, actor, origin=''):

        self.require(actor)

        if action == 'admin:input:public_proxy':
            if not self.settings.is_owner(actor.telegram_id):
                raise PermissionError('Изменение ссылки доступно администратору.')
            from app.services.public_proxy import prompt
            return prompt()

        if action == 'admin:discount' or action.startswith(('admin:discount:', 'admin:input:discount:')):
            from app.services.timed_discount import render
            return await render(self.repo, self.settings, actor, action)

        if action.startswith('admin:p:'):

            from app.services.proxies import ProxyService

            return await ProxyService(self.repo, self.settings).render(action, actor)

        if action.startswith('admin:v:'):

            from app.services.vpn import VpnService

            return await VpnService(self.repo, self.settings).render(action, actor)

        if action.startswith('admin:r:'):

            from app.services.remnawave import RemnawaveService

            return await RemnawaveService(self.repo, self.settings).render(action, actor)

        if action.startswith('admin:t:'):

            from app.services.tickets import TicketService

            return await TicketService(self.repo, self.settings).render(action, actor)

        if action.startswith('admin:x:'):

            from app.services.control import ControlService

            return await ControlService(self.repo, self.settings).render(action, actor)

        p = action.split(':')

        page = p[1]

        if page in ('proxygrant','proxycampaign') or action == 'admin:input:proxycampaign':
            from app.services.public_proxy import admin_screen, connection_url
            url = await connection_url(self.repo)
            if url and self.settings.is_owner(actor.telegram_id):
                return admin_screen(self.settings, url)

        if page=='proxycampaign' or action=='admin:input:proxycampaign':
            from app.services.proxy_campaigns import ProxyCampaigns
            campaigns=ProxyCampaigns(self.repo,self.settings)
            return await campaigns.render(action,actor) if page=='proxycampaign' else campaigns.prompt(actor)

        if page=='partnerdebt' or action.startswith('admin:input:partnerdebt:'):
            from app.services.partner_ledger import PartnerLedger
            ledger=PartnerLedger(self.repo,self.settings)
            return await (ledger.render(action,actor) if page=='partnerdebt' else ledger.prompt(action,actor))

        if page=='widgetdemo':
            from app.services.bot_widget_demo import render
            return await render(self.repo,self.settings,action,actor)

        if page=='antig':
            from app.services.bot_antig_price import AntigPrice
            return await AntigPrice(self.repo,self.settings).render(action,actor)

        if page=='saleprice':
            from app.services.bot_sale_price import SalePrice
            return await SalePrice(self.repo,self.settings).render(action,actor)

        if page=='giftlink':
            from app.services.bot_gift_links import BotGiftLinks
            return await BotGiftLinks(self.repo,self.settings).render(action,actor)
        if page=='renewcustom':
            from app.services.bot_admin_renew import BotAdminRenew
            return await BotAdminRenew(self.repo,self.settings).render(action,actor)
        if page=='proxygrant':

            from app.services.bot_proxy_grant import BotProxyGrant

            return await BotProxyGrant(self.repo,self.settings).render(action,actor)

        if page=='grantconfig':

            from app.services.bot_grant_config import BotGrantConfig

            return await BotGrantConfig(self.repo,self.settings).render(action,actor)

        if page=='clientgrant':

            from app.services.admin_client_grant import ClientGrant

            return await ClientGrant(self.repo,self.settings).render(action,actor)

        if page=='search':

            from app.services.bot_admin_search import BotAdminSearch

            return await BotAdminSearch(self.repo,self.settings).render(action,actor)

        if page=='hidden':

            from app.services.admin_hidden import render

            return await render(self.repo,p)

        if page=='clients_search':

            data=json.loads(await self.repo.setting(f'client_search:{actor.telegram_id}','{}'))

            if data.get('expires',0)<now().timestamp():return Screen('Поиск устарел. Найдите клиента заново.',[[('Поиск','admin:input:search')],back('admin:clients:0')])

            offset=max(0,int(p[2])) if len(p)>2 else 0

            ids=data.get('ids',[]);users=list(await self.session.scalars(select(User).where(User.id.in_(ids)).order_by(User.id.desc())))

            rows=[[(f'{display_freeman_id(u.freeman_id) or u.id} · {u.display_name[:25]}',f'admin:client:{u.id}')] for u in users[offset:offset+5]]

            if offset:rows.append([('← Предыдущие',f'admin:clients_search:{max(0,offset-5)}')])

            if offset+5<len(users):rows.append([('Следующие →',f'admin:clients_search:{offset+5}')])

            return Screen('🔍 Результаты поиска:' if users else 'Клиенты не найдены.',rows+[back('admin:clients:0')])

        if page=='credit':

            from sqlalchemy import update

            await self.session.execute(update(User).where(User.telegram_id==actor.telegram_id).values(status=User.status))

            data=json.loads(await self.repo.setting('balance_credit:'+p[2],'{}'))

            if data.get('actor')!=actor.telegram_id or data.get('expires',0)<now().timestamp():

                raise ValueError('Подтверждение устарело.')

            u=await self.customer(data['user'])

            if not data.get('done'):

                await self.repo.add(BalanceTransaction(user_id=u.id,type='admin_credit',amount=data['amount'],description='Ручное пополнение администратором'))

                await self.audit(actor,'balance_credit',u.id,f"{data['amount']} коп.; ручное пополнение")

                data['done']=True

                await self.repo.set_setting('balance_credit:'+p[2],json.dumps(data))

            return Screen(f'✅ Баланс клиента: {money(await self.repo.balance(u.id))}',[back(f'admin:client:{u.id}')])

        if page=='notifications':

            from app.services.admin_notifications import render

            return await render(self.repo,self.settings,actor,action)

        if page == 'attention':
            from sqlalchemy import String, cast, literal
            from app.database.models import Ticket, AppSetting as Setting
            from app.services.admin_client_grant import rich

            category = p[2] if len(p) > 2 else 'payments'
            offset = min(1000, max(0, int(p[3]))) if len(p) > 3 else 0
            link = lambda section, at=0: f'admin:attention:{section}:{at}'
            rows = [[('💳 Оплата', link('payments')), ('🔁 Автопродление', link('auto'))],
                    [('👛 Сбой с баланса', link('wallet')), ('💬 Поддержка', link('support'))]]
            if category == 'support':
                query = select(Ticket, User).join(User, User.id == Ticket.user_id).where(Ticket.status == 'waiting_admin')
                query = query.order_by(Ticket.updated_at)
                items = (await self.session.execute(query.offset(offset).limit(6))).all()
                lines = [f'#{ticket.id} · {escape(display_freeman_id(u.freeman_id))} · {escape(u.display_name[:36])}'
                         for ticket, u in items[:5]]
                rows.extend([[(f'👤 Клиент по обращению #{ticket.id}', f'admin:client:{u.id}')] for ticket, u in items[:5]])
                if items:
                    rows.append([('Открыть поддержку', 'admin:t:home')])
                title = 'Ожидают ответа'
            elif category == 'wallet':
                from app.database.models import ChatNotice
                query = select(Subscription, User).join(User, User.id == Subscription.user_id).where(
                    Subscription.expires_at <= now(), User.status == 'active',
                    select(ChatNotice.id).where(
                        ChatNotice.target_id == User.telegram_id,
                        ChatNotice.dedup.like(literal('auto:') + cast(Subscription.id, String) + literal(':%:insufficient')),
                        ChatNotice.created_at >= now() - timedelta(days=7),
                        ChatNotice.created_at >= Subscription.expires_at).exists())
                items = (await self.session.execute(query.order_by(Subscription.expires_at.desc()).offset(offset).limit(6))).all()
                title = 'Не хватило средств для автопродления за 7 дней'
                lines = [f'Подписка #{sub.id} · {escape(display_freeman_id(u.freeman_id))}' for sub,u in items[:5]]
                rows.extend([[(f'🔑 Подписка #{sub.id}', f'admin:subscription:{sub.id}')] for sub,_ in items[:5]])
            elif category in ('payments', 'auto'):
                stamp = now()
                if category == 'payments':
                    pending = Payment.status.in_(('order_processing','stars_processing','manual_received_pending'))
                    no_key = Payment.status.in_(('order_paid','stars_paid','balance_paid','manual_paid')) & Payment.subscription_id.is_(None)
                    # Legacy payments and gifts may settle without issuing a VPN key.
                    no_key &= select(Setting.key).where(
                        Setting.key == literal('sale_snapshot:') + cast(Payment.id,String),
                        Setting.value.not_like('%"gift"%')).exists()
                    from app.database.models import VlessCreation, VlessRenewal
                    from app.services.vless_creation import creation_id
                    from app.services.vless_renewal import operation_id, PENDING

                    # A remote provisioning journal can survive a rolled-back
                    # fulfillment while the invoice is still order_pending.
                    recent = stamp - timedelta(days=7)
                    candidates = list((await self.session.execute(select(Payment.id, Payment.user_id).where(
                        Payment.status == 'order_pending', Payment.created_at >= recent,
                        Payment.created_at < stamp-timedelta(minutes=5)))).all())
                    creation_rows = (await self.session.execute(select(
                        VlessCreation.id, VlessCreation.user_id, VlessCreation.customer_ref).where(
                        VlessCreation.state.in_(('outcome_unknown','remote_confirmed')), VlessCreation.updated_at >= recent))).all()
                    refs = {}
                    creation_ids = set()
                    for identity, uid, customer_ref in creation_rows:
                        refs.setdefault(uid, set()).add(customer_ref)
                        creation_ids.add(identity)
                    renewal_ids = set(await self.session.scalars(select(VlessRenewal.id).where(
                        VlessRenewal.state.in_(PENDING), VlessRenewal.updated_at >= recent)))
                    stalled_ids = [
                        pid for pid, uid in candidates
                        if operation_id(uid, f'payment:{pid}') in renewal_ids
                        or any(creation_id(ref, f'payment:{pid}') in creation_ids for ref in refs.get(uid, ()))
                    ]
                    condition = (pending | no_key | Payment.id.in_(stalled_ids)) & (Payment.created_at < stamp-timedelta(minutes=5))
                    title = 'Оплата и выдача требуют проверки'
                else:
                    card_attempt = select(Setting.key).where(Setting.key == literal('card_failure:') + cast(Payment.id,String)).exists()
                    condition = (Payment.status == 'card_uncertain') | ((Payment.status == 'order_canceled') & card_attempt)
                    title = 'Ошибки автопродления с карты за 7 дней'
                query = select(Payment, User).join(User, User.id == Payment.user_id).where(
                    condition, Payment.amount > 0, Payment.created_at >= stamp-timedelta(days=7), User.status != 'deleted')
                items = (await self.session.execute(query.order_by(Payment.id.desc()).offset(offset).limit(6))).all()
                lines = [f'Платёж #{pay.id} · {money(pay.amount)} · {escape(display_freeman_id(u.freeman_id))}' for pay,u in items[:5]]
                rows.extend([[(f'Открыть платёж #{pay.id}', f'admin:attention_order:{pay.id}:{category}:{offset}')] for pay,_ in items[:5]])
            else:
                raise ValueError('Раздел недоступен.')
            navigation = []
            if offset:
                navigation.append(('← Предыдущие', link(category, offset-5)))
            if len(items) > 5:
                navigation.append(('Следующие →', link(category, offset+5)))
            if navigation:
                rows.append(navigation)
            rows.append(back('admin:home'))
            return rich(f'🔔 <b>Требуют внимания</b>\n\n<b>{title}</b>\n\n' + ('\n'.join(lines) if lines else 'Здесь пока ничего нет.'), rows)

        if page == 'attention_order':
            from app.services.payment_history import NAMES
            pay = await self.session.get(Payment, int(p[2]))
            if not pay:
                raise ValueError('Платёж не найден.')
            category, offset = p[3], int(p[4])
            rows = [[('👤 Карточка клиента', f'admin:client:{pay.user_id}')]]
            if pay.subscription_id:
                rows.append([('🔑 Подписка', f'admin:subscription:{pay.subscription_id}')])
            rows.append(back(f'admin:attention:{category}:{offset}'))
            status = escape(NAMES.get(pay.status, {'card_uncertain':'Результат автоплатежа не подтверждён'}.get(pay.status, pay.status)))
            if category == 'payments' and pay.status == 'order_pending':
                status = 'Платёж дошёл до выдачи. Панель или запись подписки ещё проверяется.'
            return Screen(f'💳 <b>Платёж №{pay.id}</b>\n\n{money(pay.amount)}\n{status}', rows, structured=True)

        if page == 'home':

            archived = await self.archived_users()

            count_query = select(func.count()).select_from(User)

            if archived:

                count_query = count_query.where(User.id.not_in(archived))

            count = await self.session.scalar(count_query)

            role=self.settings.role(actor.telegram_id)

            if role=='support':

                return Screen('🛟 <b>Панель поддержки</b>',[[('🗂 Обращения','support_staff')],back('home')])

            if role=='manager':

                return Screen('💼 <b>Менеджер</b>\n\nБесплатное получение VPN-подписок и выдача Telegram Proxy. Доступа к клиентской базе, статистике, обращениям и выплатам нет.',[[('🔑 Получить VPN бесплатно','buy')],[('🌐 Выдать прокси','admin:p:input:grant')],back('home')],structured=True)

            return Screen(f'⚙️ <b>Администратор</b>\n\nПользователей бота: {count}\nVLESS · Remnawave — основной протокол.', [

                [('📊 Статус серверов', 'admin:r:home')],

                [('🔔 Требуют внимания', 'admin:attention')],

                [('👥 Клиенты', 'admin:clients:0')],

                [('💳 Продажи и тарифы', 'admin:commerce'), ('📣 Продвижение', 'admin:marketing')],

                [('🌐 Telegram-прокси', 'admin:p:home')],

                [('🎨 Оформление', 'admin:x:appearance')],

                [('🙈 Скрытые', 'admin:hidden')],

                [('⚙️ Дополнительно', 'admin:more')],

                back('home')])

        if page == 'commerce':

            return Screen('💳 <b>Продажи и тарифы</b>\n\nТарифы, пробный период и финансовая отчётность.', [

                [('💳 Тарифы', 'admin:tariffs'), ('🎁 Пробный период', 'admin:trial')],
                [('🏷 Скидка', 'admin:discount')],

                [('📊 Отчёт по продажам', 'admin:sales'), ('🎯 Воронка продаж', 'admin:x:funnel:30')],

                [('🛡️ Антиглушилка', 'admin:antig')],

                [('💳 Платёжные системы', 'admin:payments')],

                [('💵 Платежи и пополнения', 'admin:topups:0')],

                [('🤝 Расчёты с партнёрами', 'admin:partnerdebt')],

                [('🧾 Добавить расход', 'admin:x:input:expense')], back('admin:home')])

        if page == 'protocols':

            amnezia = await self.repo.setting('protocol_visible:amnezia', '1') == '1'

            vless = await self.repo.setting('protocol_visible:vless', '0') == '1'

            from app.database.models import RemnawaveServer

            panels = await self.session.scalar(select(func.count()).select_from(RemnawaveServer))

            text = (f'🧩 <b>Протоколы и продукты</b>\n\nAmneziaWG: {"показывается" if amnezia else "скрыт"}\n'

                    f'VLESS · Remnawave: {"показывается" if vless else "скрыт"} · панелей: {panels}\n\n'

                    'У каждого продукта свои цены, описание, приложение, инструкции и лимит устройств. VLESS по умолчанию скрыт от клиентов до подключения панели.')

            return Screen(text, [[('🟦 AmneziaWG', 'admin:protocol:amnezia')], [('🟣 VLESS · Remnawave', 'admin:protocol:vless')], back('admin:home')])

        if page == 'protocol':

            protocol = p[2]

            if protocol not in ('amnezia', 'vless'):

                raise ValueError('Протокол недоступен.')

            if protocol=='vless':

                from app.database.models import RemnawaveServer

                from app.services.document_rich import document_layout

                panels=int(await self.session.scalar(select(func.count()).select_from(RemnawaveServer)) or 0)

                ready=int(await self.session.scalar(select(func.count()).select_from(RemnawaveServer).where(RemnawaveServer.status=='active',RemnawaveServer.health=='online')) or 0)

                body=(f'🖥 Панелей подключено: {panels}\nГотовы к выдаче: {ready}\n\n'

                      '🔑 Выдача ключа\nПосле оплаты подписка создаётся через подключённую панель Remnawave. Срок и количество устройств берутся из выбранного тарифа.\n\n'

                      '📱 Подключение клиента\nКнопка «Подключить» открывает страницу подключения с инструкцией. Отдельные инструкции по приложениям в этом меню не требуются.\n\n'

                      '💰 Цены и устройства\nТарифы, цены, сроки и лимиты устройств настраиваются в разделе «Тарифы». Этот экран не меняет настройки купленных подписок.\n\n'

                      '⚙️ Управление панелями\nАдрес панели, подключение, проверка состояния и управление ключами доступны на предыдущем экране.')

                return Screen('⚙️ <b>Выдача через Remnawave</b>\n\n'+escape(body),[back('admin:r:home')],structured=True,rich=document_layout('⚙️ Выдача через Remnawave',body))

            defaults = {'amnezia': ('AmneziaWG', 'AmneziaVPN / AmneziaWG'), 'vless': ('VLESS', 'Happ')}

            title, default_app = defaults[protocol]

            visible = await self.repo.setting(f'protocol_visible:{protocol}', '1' if protocol == 'amnezia' else '0') == '1'

            app = await self.repo.setting(f'protocol_app:{protocol}', default_app)

            limit = await self.repo.setting(f'protocol_device_limit:{protocol}', '1')

            description = await self.repo.setting(f'protocol_description:{protocol}', 'Защищённое подключение на выбранный срок.')

            instruction = await self.repo.setting(f'protocol_instruction:{protocol}', 'Инструкция ещё не заполнена.')

            lines, rows = [], []

            for tariff in await self.repo.tariffs():

                price = await self.repo.tariff_setting(tariff,f'protocol_price:{protocol}',str(tariff.price))

                lines.append(f'{await self.label(tariff)}: {money(int(price))}')

                rows.append([(f'💰 {await self.label(tariff)} · {money(int(price))}', f'admin:input:protocol_price:{protocol}:id{tariff.id}')])

            text = (f'🧩 <b>{title}</b>\n\nКлиентам: {"показывается" if visible else "скрыт"}\nОсновное приложение: {escape(app)}\n'

                    f'Лимит устройств: {limit}\nОписание: {escape(description)}\nИнструкция: {escape(instruction[:500])}\n\n<b>Цены</b>\n' + '\n'.join(lines))

            controls = [[('✏️ Описание', f'admin:input:protocol_description:{protocol}')],

                        [('📱 Основное приложение', f'admin:input:protocol_app:{protocol}')],

                        [('📲 Лимит устройств', f'admin:input:protocol_device_limit:{protocol}')],

                        [('📖 Инструкция', f'admin:input:protocol_instruction:{protocol}')]]

            if protocol == 'vless':

                from app.database.models import RemnawaveServer

                panels = await self.session.scalar(select(func.count()).select_from(RemnawaveServer))

                ready = await self.session.scalar(select(func.count()).select_from(RemnawaveServer).where(RemnawaveServer.status == 'active', RemnawaveServer.health == 'online'))

                apps = (('happ', 'Happ'), ('v2raytun', 'v2RayTun'), ('incy', 'Incy'))

                app_lines = []

                for slug, label in apps:

                    filled = bool(await self.repo.setting(f'protocol_instruction:vless:{slug}'))

                    app_lines.append(f'{label}: {"заполнена" if filled else "не заполнена"}')

                    controls.append([(f'📖 Инструкция · {label}', f'admin:input:protocol_instruction_app:vless:{slug}')])

                text += f'\n\n<b>Приложения VLESS</b>\n' + '\n'.join(app_lines)

                text += f'\n\nПанелей Remnawave: {panels} · готовы к выдаче: {ready}'

                if ready or visible:

                    controls.insert(0, [(('🙈 Скрыть' if visible else '👁 Показать клиентам'), f'admin:protocoltoggle:{protocol}:visible')])

                controls.append([('🖥 Панели Remnawave', 'admin:r:home')])

            else:

                controls.insert(0, [(('🙈 Скрыть' if visible else '👁 Показать клиентам'), f'admin:protocoltoggle:{protocol}:visible')])

            return Screen(text, controls + rows + [back('admin:protocols')])

        if page == 'protocoltoggle':

            protocol, operation = p[2], p[3]

            if protocol not in ('amnezia', 'vless') or operation != 'visible':

                raise ValueError('Настройка недоступна.')

            key = f'protocol_visible:{protocol}'

            current = await self.repo.setting(key, '1' if protocol == 'amnezia' else '0')

            if protocol == 'vless' and current != '1':

                from app.database.models import RemnawaveServer

                ready = await self.session.scalar(select(func.count()).select_from(RemnawaveServer).where(RemnawaveServer.status == 'active', RemnawaveServer.health == 'online'))

                if not ready:

                    raise ValueError('Сначала подключите и проверьте хотя бы одну панель Remnawave. До этого VLESS останется скрытым от клиентов.')

            await self.repo.set_setting(key, '0' if current == '1' else '1')

            await self.audit(actor, 'protocol_visibility', protocol, f'{current} → {"0" if current == "1" else "1"}')

            return await self.render(f'admin:protocol:{protocol}', actor)

        if page == 'payments':

            stars = await self.repo.setting('payment_provider:stars', '0') == '1'

            yookassa = await self.repo.setting('payment_provider:yookassa', '1') == '1'

            yk_ready = bool(self.settings.yookassa_shop_id and self.settings.yookassa_secret_key)

            robo_ready = bool(self.settings.robokassa_merchant_login and self.settings.robokassa_password1 and self.settings.robokassa_password2 and self.settings.robokassa_result_url.startswith('https://'))

            text = ('💳 <b>Платёжные системы</b>\n\n'

                    f'Telegram Stars: {"🟢 включены" if stars else "⚪ выключены"}\n'

                    f'ЮKassa: {"🟢 включена" if yookassa and yk_ready else "⚪ выключена" if yk_ready else "🔴 не настроена"}\n'

                    f'Robokassa: {"🟡 реквизиты заполнены" if robo_ready else "🔴 не настроена"}\n\n'

                    'Цена Stars рассчитывается из цены в рублях. ЮKassa используется для СБП и пополнения баланса. Robokassa разрешена только во внешнем HTTPS-магазине: цифровые услуги внутри Telegram должны оплачиваться Stars.')

            return Screen(text, [[('⭐ Telegram Stars', 'admin:stars')], [('🏦 ЮKassa', 'admin:yookassa')], [('🟠 Robokassa', 'admin:robokassa')], back('admin:commerce')])

        if page == 'stars':
            from app.services.bot_catalog import available, catalog_term, devices_label
            from app.services.bot_antig_price import tariff_total
            from app.services.stars import configured_stars, star_amount

            tariffs = await available(self.repo)
            if len(p) == 2:
                enabled = await self.repo.setting('payment_provider:stars', '0') == '1'
                toggle = '🔴 Выключить' if enabled else '🟢 Включить'
                status = 'Включена' if enabled else 'Выключена'
                return Screen(f'⭐ <b>Telegram Stars</b>\n\nОплата: {status}\nВыберите количество устройств.',
                              [[(devices_label(n), f'admin:stars:devices:{n}')] for n in sorted({t.device_limit for t in tariffs})]
                              + [[(toggle, 'admin:paymenttoggle:stars')], back('admin:payments')])
            if len(p) == 4 and p[2] == 'devices':
                count = self.integer(p[3], 1, 100)
                selected = [t for t in tariffs if t.device_limit == count]
                rows = []
                for tariff in selected:
                    title = catalog_term(tariff.months)
                    if sum(t.months == tariff.months for t in selected) > 1:
                        title += f' · №{tariff.id}'
                    rows.append([(f'{title} · {money(tariff.price)}', f'admin:stars:tariff:{tariff.id}')])
                return Screen(f'⭐ <b>{devices_label(count)}</b>\n\nВыберите срок.', rows + [back('admin:stars')])
            if len(p) != 4 or p[2] != 'tariff':
                raise ValueError('Настройка недоступна.')
            tariff = next((t for t in tariffs if str(t.id) == p[3]), None)
            if not tariff:
                raise ValueError('Тариф недоступен.')
            lines = [f'⭐ <b>{escape(await self.label(tariff))}</b>']
            rows = []
            for addon, label, amount in ((False, 'Подписка', tariff.price),
                                         (True, 'Антиглушилка', await tariff_total(self.repo, tariff, discounted=False))):
                override = await configured_stars(self.repo, tariff, addon=addon)
                mode = 'вручную' if override is not None else 'авто'
                stars = await star_amount(self.repo, tariff, amount, addon=addon)
                lines.append(f'{label}: {money(amount)} → <b>{stars} ⭐</b> · {mode}')
                kind = 'stars_antig_price' if addon else 'stars_price'
                rows.append([(f'✏️ {label}', f'admin:input:{kind}:id{tariff.id}')])
            lines.append('В режиме «авто» цена меняется вместе с рублёвой. Скидки учитываются при оформлении.')
            return Screen('\n\n'.join(lines), rows + [back(f'admin:stars:devices:{tariff.device_limit}')])

        if page == 'paymenttoggle':

            provider = p[2]

            if provider not in ('stars', 'yookassa'):

                raise ValueError('Платёжная система недоступна.')

            if provider == 'yookassa' and not (self.settings.yookassa_shop_id and self.settings.yookassa_secret_key):

                raise ValueError('Сначала откройте ЮKassa и добавьте реквизиты через кнопку «Настроить в Telegram».')

            key = f'payment_provider:{provider}'

            current = await self.repo.setting(key, '1' if provider == 'yookassa' else '0')

            new = '0' if current == '1' else '1'

            await self.repo.set_setting(key, new)

            await self.audit(actor, 'payment_provider', provider, f'{current} → {new}')

            return await self.render('admin:stars' if provider == 'stars' else 'admin:yookassa', actor)

        if page == 'robokassa':

            configured = bool(self.settings.robokassa_merchant_login and self.settings.robokassa_password1 and self.settings.robokassa_password2 and self.settings.robokassa_result_url.startswith('https://'))

            merchant = ('••••' + self.settings.robokassa_merchant_login[-4:]) if self.settings.robokassa_merchant_login else 'не задан'

            text = (f'🟠 <b>Robokassa</b>\n\nСтатус: {"реквизиты заполнены" if configured else "не настроена"}\nМагазин: {merchant}\n'

                    'Robokassa подключается к внешнему HTTPS-магазину с проверяемым ResultURL. Внутри Telegram цифровая подписка должна продаваться через Stars, поэтому кнопка Robokassa в карточке тарифа не создаётся.')

            if not configured:

                text += '\n\nМожно безопасно добавить реквизиты прямо здесь. Сообщение с паролями бот удалит, а значения сохранит в базе в зашифрованном виде.'

            return Screen(text, [[('🔐 Настроить в Telegram', 'admin:input:payment_robokassa')], back('admin:payments')])

        if page=='topups':

            from app.services.payment_history import render_payments

            return await render_payments(self.repo, p)

        if page in ('yookassa','yookassatest'):

            from app.services.topups import YooKassa

            configured = bool(self.settings.yookassa_shop_id and self.settings.yookassa_secret_key)

            checked = False

            error = ''

            if page == 'yookassatest' and configured:

                try:

                    checked = await YooKassa(self.settings).check()

                except ValueError as exception:

                    error = str(exception)

            shop = ('••••' + self.settings.yookassa_shop_id[-4:]) if self.settings.yookassa_shop_id else 'не задан'

            mode = 'тестовый' if self.settings.yookassa_test_mode else 'боевой'

            enabled = await self.repo.setting('payment_provider:yookassa', '1') == '1'

            text = (f'🏦 <b>ЮKassa</b>\n\nСтатус настроек: {"заполнены" if configured else "не подключена"}\nПриём платежей: {"включён" if enabled and configured else "выключен"}\nМагазин: {shop}\nРежим: {mode}\n'

                    'Зачисление выполняется только после серверной проверки статуса, суммы, валюты, пользователя и идентификатора платежа. Секретный ключ в интерфейсе не показывается.')

            if checked:

                text += '\n\n✅ Авторизация в API проверена успешно.'

            elif error:

                text += '\n\n❌ Проверка API: ' + escape(error)

            if not configured:

                text += '\n\nРеквизиты можно добавить прямо в этом чате. Сообщение с ключом бот удалит после обработки, а значения сохранит в базе в зашифрованном виде.'

            rows = [[('🔐 Настроить в Telegram','admin:input:payment_yookassa')]]

            if configured:

                rows += [[('🔄 Проверить API','admin:yookassatest')], [(('🔴 Выключить' if enabled else '🟢 Включить'), 'admin:paymenttoggle:yookassa')]]

            return Screen(text,rows+[back('admin:payments')])

        if page == 'marketing':

            return Screen('📣 <b>Продвижение</b>\n\nРассылки, промокоды и реферальная программа.', [

                [('📣 Рассылки', 'admin:x:campaigns:0'), ('🎟 Промокоды', 'admin:promos')],

                [('👥 Реферальная программа', 'admin:referrals'), ('💸 Выплаты', 'admin:x:withdrawals:0')],

                [('🔔 Напоминания', 'admin:x:reminders')], back('admin:home')])

        if page == 'more':

            return Screen('⚙️ <b>Дополнительно</b>\n\nУправление доступом сотрудников и резервными копиями.', [[('🧑‍💼 Сотрудники и доступ','admin:roles')],[('🔔 Уведомления','admin:notifications')],[('🛡 Мониторинг и копии','admin:x:monitor')],back('admin:home')],structured=True)

        if page == 'clients':

            offset = max(0, int(p[2]))

            query = select(User)

            archived = await self.archived_users()

            if archived:

                query = query.where(User.id.not_in(archived))

            customers = list(await self.session.scalars(query.order_by(User.id.desc()).offset(offset).limit(6)))

            total_query = select(func.count()).select_from(User)

            if archived:

                total_query = total_query.where(User.id.not_in(archived))

            total = await self.session.scalar(total_query)

            new_today = await self.session.scalar(select(func.count()).select_from(User).where(User.created_at >= now()-timedelta(days=1), *([User.id.not_in(archived)] if archived else [])))

            rows = [[(f'{display_freeman_id(u.freeman_id) or u.id} · {u.display_name[:25]}', f'admin:client:{u.id}')] for u in customers[:5]]

            nav = []

            if offset:

                nav.append(('← Предыдущие', f'admin:clients:{max(0, offset-5)}'))

            if len(customers) > 5:

                nav.append(('Следующие →', f'admin:clients:{offset+5}'))

            text = f'👥 <b>Клиенты</b>\n\nВсего: {total}\n\nНовых за 24 часа: {new_today}\n\nСначала показаны последние регистрации.' if customers else 'Клиентов пока нет.'

            return Screen(text, rows + ([nav] if nav else []) + [[('🔄 Обновить', f'admin:clients:{offset}'), ('🔍 Поиск', 'admin:input:search')], [('📤 Выгрузить базу', 'admin:x:exports:0')], back('admin:home')],structured=True)

        if page in ('roles','staff','staffrole','staffblock','staffremove','staffdelete'):

            from app.services.staff_admin import render

            return await render(self,action,actor)

        if page == 'client':

            u = await self.customer(p[2])

            subscriptions = await self.repo.subscriptions(u.id)

            email = u.email or await self.repo.setting(f'user_email:{u.id}', '—')

            telegram=str(u.telegram_id) if u.telegram_id and u.telegram_id>0 else 'Не привязан'

            fid=display_freeman_id(u.freeman_id) or f'Freeman {u.id}'

            status={'active':'Активен','blocked':'Заблокирован'}.get(u.status,u.status)

            active=sum(item.expires_at.replace(tzinfo=timezone.utc)>now() for item in subscriptions)

            text=(f'👤 <b>{escape(u.display_name)}</b>\n\n'

                  f'🆔 <b>Freeman ID</b>\n<code>{escape(fid)}</code>\n\n'

                  f'✈️ <b>Telegram ID</b>\n{("<code>"+telegram+"</code>") if telegram!="Не привязан" else telegram}\n\n'

                  f'📧 <b>Email</b>\n{escape(email)}\n\n'

                  f'<blockquote>📋 <b>Аккаунт</b>\n\n'

                  f'Статус: {escape(status)}\n\n'

                  f'Имя в Telegram: {escape("@"+u.username if u.username else "—")}\n\n'

                  f'Зарегистрирован: {u.created_at:%d.%m.%Y}\n\n'

                  f'Почта подтверждена: {"Да" if u.email_verified else "Нет"}</blockquote>\n\n'

                  f'💰 <b>Баланс:</b> {money(await self.repo.balance(u.id))}\n\n'

                  f'🔑 <b>Активных подписок:</b> {active}\n\n'

                  'Ключи, покупки, устройства и история доступны по кнопкам ниже.')

            screen=Screen(text,[],structured=True,avatar_telegram_id=u.telegram_id if u.telegram_id and u.telegram_id>0 else None)

            screen.rows += [[('💬 Связаться с клиентом',f'url:tg://user?id={u.telegram_id}')],[('🎁 Выдать ключ',f'admin:issue:{u.id}')],[('🔑 Ключи',f'admin:keys:{u.id}'),('📦 Подписки',f'admin:subs:{u.id}')],[('🧾 Покупки',f'admin:purchases:{u.id}:0'),('📜 Баланс',f'admin:ledger:{u.id}')],[('👥 Приглашения',f'admin:invites:{u.id}:0')],[('🛑 Блокировка / разблокировка',f'admin:confirm:block:{u.id}')],back('admin:clients:0')]

            if not u.telegram_id: screen.rows=[row for row in screen.rows if not any(action.startswith('url:tg://user') for _,action in row)]

            screen.rows.insert(1,[('🏷 Скидка и специальные цены',f'admin:clientprices:{u.id}')])

            screen.rows.insert(2,[('👥 Реферальные условия',f'admin:clientref:{u.id}')])

            screen.rows.insert(3,[('➕ Пополнить баланс',f'admin:input:balance_credit:{u.id}')])

            return screen

        if page=='clientref':

            from app.services.personal_referrals import PersonalReferrals

            return await PersonalReferrals(self.repo,self.settings).render(action,actor)

        if page=='clientprices':

            u=await self.customer(p[2])

            text=f'🏷 Цены клиента {u.telegram_id}\nПостоянная скидка: {await self.repo.setting(f"client_discount:{u.id}","0")}%\n\nИндивидуальная цена тарифа имеет приоритет. Без неё применяется большая из постоянной скидки и скидки по промокоду. Скидки не складываются.'

            rows=[[('✏️ Постоянная скидка',f'admin:input:client_discount:{u.id}')]]

            for tariff in await self.repo.tariffs():

                value=await self.repo.tariff_setting(tariff,f'client_price:{u.id}')

                rows.append([(f'{await self.label(tariff)} · {money(int(value)) if value else "Обычная цена"}',f'admin:input:client_price:{u.id}:id{tariff.id}')])

            return Screen(text,rows+[back(f'admin:client:{u.id}')])

        if page == 'purchases':

            u = await self.customer(p[2])

            offset = max(0, int(p[3]))

            payments = list(await self.session.scalars(select(Payment).where(Payment.user_id == u.id).order_by(Payment.id.desc()).offset(offset).limit(6)))

            text = '\n'.join(f'#{pay.id} · {pay.created_at:%d.%m.%Y} · {money(pay.amount)} · {escape(pay.status)}' for pay in payments[:5]) or 'Покупок пока нет.'

            rows = []

            if offset:

                rows.append([('← Предыдущие', f'admin:purchases:{u.id}:{max(0, offset-5)}')])

            if len(payments) > 5:

                rows.append([('Следующие →', f'admin:purchases:{u.id}:{offset+5}')])

            return Screen('🧾 <b>История покупок</b>\n\n' + text, rows + [back(f'admin:client:{u.id}')])

        if page == 'ledger':

            u = await self.customer(p[2])

            offset=max(0,int(p[3])) if len(p)>3 else 0

            transactions=list(await self.session.scalars(select(BalanceTransaction).where(BalanceTransaction.user_id==u.id).order_by(BalanceTransaction.id.desc()).offset(offset).limit(6)))

            text = '\n'.join(f'#{t.id} · {money(t.amount)} · {escape(t.description)}' for t in transactions[:5]) or 'Операций нет.'

            rows=[]

            if offset:rows.append([('← Предыдущие',f'admin:ledger:{u.id}:{max(0,offset-5)}')])

            if len(transactions)>5:rows.append([('Следующие →',f'admin:ledger:{u.id}:{offset+5}')])

            return Screen('Операции баланса:\n\n'+text,rows+[back(f'admin:client:{u.id}')])

        if page == 'invites':

            from app.database.models import Referral

            u = await self.customer(p[2])

            offset = max(0, int(p[3]))

            invites = list(await self.session.scalars(select(User).join(Referral, Referral.invitee_id == User.id).where(Referral.inviter_id == u.id).order_by(Referral.id.desc()).offset(offset).limit(6)))

            rows = [[(f'{display_freeman_id(v.freeman_id) or v.id} · {v.display_name[:25]}', f'admin:client:{v.id}')] for v in invites[:5]]

            if offset:

                rows.append([('← Предыдущие', f'admin:invites:{u.id}:{max(0, offset-5)}')])

            if len(invites) > 5:

                rows.append([('Следующие →', f'admin:invites:{u.id}:{offset+5}')])

            return Screen('👥 Приглашённые клиенты' if invites else 'Приглашений нет.', rows + [back(f'admin:client:{u.id}')])

        if page == 'subs':

            u = await self.customer(p[2])

            subscriptions = await self.repo.subscriptions(u.id)

            return Screen('📦 Подписки клиента', [[(await self.repo.setting(f'subscription_name:{s.id}', f'Подписка #{s.id}'), f'admin:subscription:{s.id}')] for s in subscriptions] + [back(f'admin:client:{u.id}')])

        if page == 'subscription':

            s = await self.session.get(Subscription, int(p[2]))

            if not s:

                raise ValueError('Подписка не найдена.')

            status = await self.repo.setting(f'subscription_status:{s.id}', 'active')

            name = escape(await self.repo.setting(f'subscription_name:{s.id}', f'Подписка #{s.id}'))

            return Screen(f'Подписка #{s.id}\nНазвание: {name}\nТип: {s.kind}\nСтатус: {status}\nДо: {s.expires_at:%d.%m.%Y %H:%M} UTC\nЛимит: {s.device_limit}', [[('➕ Добавить дни', f'admin:input:extend:{s.id}')], back(f'admin:subs:{s.user_id}')])

        if page == 'keys':

            u = await self.customer(p[2])

            from app.database.models import VlessCredential
            from app.services.subscription_labels import key_row
            offset=max(0,int(p[3])) if len(p)>3 else 0
            base=select(AccessKey).where(AccessKey.user_id==u.id,AccessKey.status!='deleted')
            total=await self.session.scalar(select(func.count()).select_from(base.subquery()))
            offset=min(offset,max(0,(total-1)//5*5))
            keys=list(await self.session.scalars(base.order_by(AccessKey.id.desc()).offset(offset).limit(5)))
            mapping=dict((await self.session.execute(select(VlessCredential.access_key_id,Subscription).join(Subscription,Subscription.id==VlessCredential.subscription_id).where(VlessCredential.access_key_id.in_([k.id for k in keys])))).all()) if keys else {}
            rows=[[(await key_row(self.repo,k,mapping.get(k.id)),f'admin:key:{k.id}')] for k in keys]
            if offset:rows.append([('← Предыдущие',f'admin:keys:{u.id}:{offset-5}')])
            if offset+5<total:rows.append([('Следующие →',f'admin:keys:{u.id}:{offset+5}')])
            rows+=[[('🎁 Выдать ключ',f'admin:issue:{u.id}')],back(f'admin:client:{u.id}')]
            return Screen('🔑 <b>Ключи клиента</b>\n\n'+('Выберите подписку. Срок рассчитан на текущий момент.' if keys else 'Ключей пока нет.'),rows,structured=True)

        if page == 'issue':

            from app.services.admin_client_grant import ClientGrant

            return await ClientGrant(self.repo,self.settings).render(action,actor)

        if page == 'legacy_issue':

            from app.database.models import RemnawaveServer

            panel=await self.session.scalar(select(RemnawaveServer).where(RemnawaveServer.status=='active').order_by(RemnawaveServer.priority))

            if panel:

                return Screen('Выдача VLESS через Remnawave. Укажите Freeman ID клиента, дни, устройства и название.',[[('➕ Выдать VLESS',f'admin:r:input:grant:{panel.id}')],[('← Назад',f'admin:client:{p[2]}')]])

            u = await self.customer(p[2])

            if u.status == 'blocked':

                raise ValueError('Сначала разблокируйте клиента.')

            return Screen(f'🎁 <b>Выдать ключ</b>\n\nКлиент: {u.telegram_id} · {escape(u.display_name)}\nВыберите новый ключ или уже существующий.\nСейчас доступны тестовые записи.', [[('➕ Создать новый', f'admin:input:key:{u.id}')], [('🔑 Выбрать существующий', f'admin:keypick:{u.id}:0')], [('🔍 Найти ключ по ID', f'admin:input:key_id:{u.id}')], back(f'admin:client:{u.id}')])

        if page == 'keypick':

            u = await self.customer(p[2])

            if u.status == 'blocked':

                raise ValueError('Клиент заблокирован.')

            offset = max(0, int(p[3]))

            keys = list(await self.session.scalars(select(AccessKey).where(AccessKey.user_id != u.id,

                AccessKey.status != 'deleted', AccessKey.mode == 'mock', AccessKey.expires_at > now()).order_by(AccessKey.id.desc()).offset(offset).limit(6)))

            rows = [[(f'#{k.id} · {k.name[:25]} · {k.status}', f'admin:keyassign:{k.id}:{u.id}')] for k in keys[:5]]

            if offset:

                rows.append([('← Предыдущие', f'admin:keypick:{u.id}:{max(0, offset-5)}')])

            if len(keys) > 5:

                rows.append([('Следующие →', f'admin:keypick:{u.id}:{offset+5}')])

            return Screen('🔑 Выберите ключ. Передача от другого клиента потребует подтверждения.' if keys else 'Подходящих ключей нет. Создайте новый.', rows + [[('➕ Создать новый', f'admin:input:key:{u.id}')], back(f'admin:issue:{u.id}')])

        if page == 'keyassign':

            return await self.assignment_preview(int(p[2]), int(p[3]), actor)

        if page == 'keyapply':

            from app.database.models import AppSetting

            from app.services.keys import KeyService

            key_id, customer_id, token = int(p[2]), int(p[3]), p[4]

            stored = await self.repo.setting('confirm:' + token)

            if not stored:

                raise ValueError('Подтверждение уже использовано или недоступно.')

            confirmation = json.loads(stored)

            if confirmation.get('actor') != actor.telegram_id or confirmation.get('operation') != 'keyassign' or confirmation.get('key_id') != key_id or confirmation.get('customer_id') != customer_id or confirmation.get('expires', 0) < now().timestamp():

                raise ValueError('Подтверждение недоступно или истекло.')

            claimed = await self.session.execute(sql_delete(AppSetting).where(AppSetting.key == 'confirm:' + token, AppSetting.value == stored))

            if claimed.rowcount != 1:

                raise ValueError('Подтверждение уже использовано.')

            await KeyService(self.repo, self.settings).assign(actor, key_id, customer_id, confirmation['expected_owner'], confirmation['expected_status'])

            return Screen('✅ Ключ назначен клиенту.\nОн увидит запись в «Мои подписки» → «Мои ключи».\nРеальное подключение пока не создано.', [[('🔑 Ключи клиента', f'admin:keys:{customer_id}')], back(f'admin:client:{customer_id}')])

        if page == 'key':

            from app.database.models import VlessCredential

            c=await self.session.scalar(select(VlessCredential).where(VlessCredential.access_key_id==int(p[2])))

            if c and c.status!='deleted':

                from app.services.remnawave import RemnawaveService

                from app.services.admin_key_navigation import route

                return await RemnawaveService(self.repo,self.settings).render(route(f'admin:r:key:{c.id}',origin or f'c{c.user_id}'),actor)

            key = await self.session.get(AccessKey, int(p[2]))

            if not key:

                raise ValueError('Ключ не найден.')

            return Screen(f'🔑 <b>{escape(key.name)}</b>\n\nID: {key.id}\nСтатус: {key.status}\nРежим: тестовый\nДо: {key.expires_at:%d.%m.%Y %H:%M} UTC', ([ [('🗑 Удалить', f'admin:confirm:keydelete:{key.id}')] ] if key.status != 'deleted' else []) + [back(f'admin:keys:{key.user_id}')])

        if page == 'tariffs':

            tariffs = await self.repo.tariffs()

            tariffs=[t for t in tariffs if await self.repo.tariff_setting(t, 'tariff_deleted')!='1']

            return Screen('💳 <b>Тарифы</b>', [[(f'{await self.label(t)} · {t.device_limit} устр. · {money(t.price)}', f'admin:tariff:id{t.id}')] for t in tariffs] + [[('➕ Добавить тариф','admin:input:tariff_add')],back('admin:commerce')])

        if page == 'tariff':

            t = await self.repo.tariff(p[2])

            if not t:

                raise ValueError('Тариф не найден.')

            enabled = await self.repo.tariff_setting(t, 'tariff_enabled', '1')

            return Screen(f'<b>{escape(await self.label(t))}</b>\nЦена: {money(t.price)}\nЛимит устройств: {t.device_limit}\nВ продаже: {"Да" if enabled == "1" else "Нет"}', [[('✏️ Название', f'admin:input:tariff_name:id{t.id}')], [('💰 Цена', f'admin:input:tariff_price:id{t.id}')], [('📱 Лимит устройств', f'admin:input:tariff_limit:id{t.id}')], [('Включить / выключить', f'admin:confirm:tarifftoggle:id{t.id}')], [('🗑 Удалить тариф',f'admin:confirm:tariffdelete:id{t.id}')], back('admin:tariffs')])

        if page == 'trial':

            from app.services.trial_offer import trial_hours
            hours = await trial_hours(self.repo)

            enabled = await self.repo.setting('trial_enabled', '1')

            return Screen(f'🎁 <b>Пробный период</b>\n\nСрок: {hours} ч.\nСтатус: {"🟢 Включён" if enabled == "1" else "🔴 Выключен"}\nКнопка показывается новым клиентам только в разделе покупки. После использования она исчезает.', [[('✏️ Изменить срок', 'admin:input:setting:trial_hours')], [(('🔴 Выключить' if enabled == '1' else '🟢 Включить'), 'admin:confirm:trialtoggle:0')], back('admin:commerce')])

        if page == 'referrals':

            from app.services.marketing_settings import referral_screen

            return await referral_screen(self.repo)

        if page=='promotoggle':

            await self.repo.set_setting('promos_enabled','0' if await self.repo.setting('promos_enabled','1')=='1' else '1')

            return await self._render('admin:promos',actor)

        if page == 'promos':

            offset=max(0,int(p[2])) if len(p)>2 else 0

            promos=list(await self.session.scalars(select(PromoCode).order_by(PromoCode.id).offset(offset).limit(6)))

            nav=[]

            if offset:nav.append([('← Предыдущие',f'admin:promos:{max(0,offset-5)}')])

            if len(promos)>5:nav.append([('Следующие →',f'admin:promos:{offset+5}')])

            text = '\n'.join([f'{escape(promo.code)} · {promo.value}% · {"вкл" if await self.repo.setting(f"promo_enabled:{promo.code}", "1") == "1" else "выкл"}' for promo in promos[:5]])

            return Screen('🎟 <b>Промокоды</b>\n\nФорма у клиентов: '+('включена' if await self.repo.setting('promos_enabled','1')=='1' else 'выключена')+'\n\n' + text, nav+[[('👁 Показать / скрыть форму у клиентов','admin:promotoggle')],[('➕ Создать / изменить', 'admin:input:promo')], [('Включить / выключить код', 'admin:input:promo_toggle')], back('admin:marketing')])

        if page == 'servers':

            offset=max(0,int(p[2])) if len(p)>2 else 0

            servers=list(await self.session.scalars(select(Server).order_by(Server.id).offset(offset).limit(6)))

            nav=[]

            if offset:nav.append([('← Предыдущие',f'admin:servers:{max(0,offset-5)}')])

            if len(servers)>5:nav.append([('Следующие →',f'admin:servers:{offset+5}')])

            text = '\n'.join(f'#{s.id} · {escape(s.name)} · {escape(s.address)}\nСтатус: мониторинг не подключён' for s in servers[:5]) or 'Серверы ещё не добавлены.'

            return Screen('🖥 <b>Серверы</b>\n\n' + text + '\n\nОнлайн-статус, нагрузка и доступность появятся после подключения мониторинга.', nav+[[('➕ Добавить в реестр', 'admin:input:server')], back('admin:more')])

        if page == 'sales':

            from app.services.control import ControlService

            return await ControlService(self.repo, self.settings).report(7)

        if page == 'buttons':

            rows=[[(title,f'admin:button_group:{name}:0')] for name,(title,keys) in BUTTON_GROUPS.items() if keys]

            return Screen('✏️ <b>Кнопки по разделам</b>\n\nВыберите раздел, затем кнопку. Доступны текст, кастомная иконка, цвет, видимость и порядок.',rows+[[('➕ Свои кнопки','admin:custombuttons')],back('admin:x:appearance')])

        if page=='button_group':

            group=p[2];offset=max(0,int(p[3])) if len(p)>3 else 0

            if group not in BUTTON_GROUPS:raise ValueError('Раздел недоступен.')

            title,keys=BUTTON_GROUPS[group]

            rows=[[(await self.repo.setting('button:'+key,EDITABLE_BUTTONS[key]),f'admin:button:{key}')] for key in keys[offset:offset+5]]

            if offset:rows.append([('← Предыдущие',f'admin:button_group:{group}:{max(0,offset-5)}')])

            if offset+5<len(keys):rows.append([('Следующие →',f'admin:button_group:{group}:{offset+5}')])

            return Screen('<b>'+escape(title)+'</b>\n\nНастройки применяются ко всем кнопкам этого типа, включая новые ключи и устройства.',rows+[back('admin:buttons:0')])

        if page=='custombuttons':

            values=await self.repo.settings_prefix('custom_button:');items=[]

            for key,raw in values.items():

                try: item=json.loads(raw)

                except (json.JSONDecodeError,TypeError): continue

                if item:

                    items.append((int(item.get('position',1000)),key.split(':',1)[1],item))

            items.sort(key=lambda row:(row[0],row[1]))

            rows=[[(f'{"🟢" if item.get("visible",True) else "⚪"} {str(item.get("label","Кнопка"))[:45]}',f'admin:custombutton:{token}')]

                  for _,token,item in items]

            rows.insert(0,[('➕ Добавить кнопку','admin:input:custom_button')])

            return Screen('➕ <b>Свои кнопки</b>\n\nСоздавайте ссылки и переходы в нужных разделах.\n\nФормат: Название | экран | переход\nЭкраны: '+', '.join(CUSTOM_BUTTON_LOCATIONS),rows+[back('admin:buttons:0')])

        if page=='custombutton':

            token=p[2];item=await self.custom_button(token);visible=item.get('visible',True)

            place=CUSTOM_BUTTON_LOCATIONS.get(item.get('page'),item.get('page','—'))

            return Screen(f'🔘 <b>Своя кнопка</b>\n\nНазвание: {escape(str(item.get("label","")))}\nЭкран: {escape(place)}\nПереход: {escape(str(item.get("target","")))}\nПозиция: {item.get("position",1000)}\nСтатус: {"показывается" if visible else "скрыта"}',[

                [('🙈 Скрыть' if visible else '👁 Показать',f'admin:customtoggle:{token}')],

                [('✏️ Название',f'admin:input:custom_button_label:{token}'),('📍 Экран',f'admin:input:custom_button_page:{token}')],

                [('↪️ Переход',f'admin:input:custom_button_target:{token}'),('🔢 Позиция',f'admin:input:custom_button_position:{token}')],

                [('🗑 Удалить',f'admin:customdeleteconfirm:{token}')],back('admin:custombuttons')])

        if page=='customtoggle':

            token=p[2];item=await self.custom_button(token);item['visible']=not item.get('visible',True)

            await self.repo.set_setting('custom_button:'+token,json.dumps(item,ensure_ascii=False));await self.audit(actor,'custom_button_visible',token,item['visible'])

            return await self._render(f'admin:custombutton:{token}',actor)

        if page=='customdeleteconfirm':

            token=p[2];item=await self.custom_button(token)

            return Screen(f'Удалить кнопку «{escape(str(item.get("label","Кнопка")))}»?',[[('✅ Удалить',f'admin:customdelete:{token}')],back(f'admin:custombutton:{token}')])

        if page=='customdelete':

            token=p[2];item=await self.custom_button(token)

            await self.repo.set_setting('custom_button:'+token,'');await self.audit(actor,'custom_button_delete',token,item.get('label',''))

            return Screen('✅ Кнопка удалена.',[back('admin:custombuttons')])

        if page=='button':

            key=p[2]

            if key not in EDITABLE_BUTTONS:

                raise ValueError('Кнопка недоступна.')

            label=await self.repo.setting('button:'+key,EDITABLE_BUTTONS[key])

            icon=await self.repo.setting('button_icon:'+key)

            style=await self.repo.setting('button_style:'+key)

            visible=await self.repo.setting('button_visible:'+key,'1')=='1'

            style_name={'':'Обычный','primary':'Синий','success':'Зелёный','danger':'Красный'}.get(style,'Обычный')

            return Screen(f'🔘 <b>Кнопка</b>\n\nТекст: {escape(label)}\nОтображение: {"показывается" if visible else "скрыта"}\nКастомная иконка: {icon or "нет"}\nЦвет: {style_name}',[[('🙈 Скрыть' if visible else '👁 Показать',f'admin:buttonvisible:{key}')],[('⬆️ Выше',f'admin:buttonmove:{key}:up'),('⬇️ Ниже',f'admin:buttonmove:{key}:down')],[('✏️ Изменить текст',f'admin:input:button:{key}')],[('🌟 Установить / убрать иконку',f'admin:input:button_icon:{key}')],[('Обычный',f'admin:buttonstyle:{key}:default'),('Синий',f'admin:buttonstyle:{key}:primary')],[('Зелёный',f'admin:buttonstyle:{key}:success'),('Красный',f'admin:buttonstyle:{key}:danger')],[('↩️ Восстановить оформление',f'admin:buttonreset:{key}')],back(f'admin:button_group:{button_group(key)}:0')])

        if page=='buttonreset':

            from app.database.models import AppSetting as ButtonSetting

            key=p[2]

            if key not in EDITABLE_BUTTONS:raise ValueError('Кнопка недоступна.')

            for prefix in ('button:','button_icon:','button_style:','button_position:'):

                await self.session.execute(sql_delete(ButtonSetting).where(ButtonSetting.key==prefix+key))

            await self.repo.set_setting('button_visible:'+key,'1')

            await self.audit(actor,'button_reset',key,'Default appearance restored')

            return await self._render(f'admin:button:{key}',actor)

        if page=='buttonvisible':

            key=p[2]

            if key not in EDITABLE_BUTTONS: raise ValueError('Кнопка недоступна.')

            old=await self.repo.setting('button_visible:'+key,'1');new='0' if old=='1' else '1'

            await self.repo.set_setting('button_visible:'+key,new)

            await self.audit(actor,'button_visible',key,f'{old} → {new}')

            return await self._render(f'admin:button:{key}',actor)

        if page=='buttonmove':

            key,direction=p[2],p[3]

            if key not in EDITABLE_BUTTONS or direction not in ('up','down'): raise ValueError('Кнопка недоступна.')

            default={name:index for index,name in enumerate(EDITABLE_BUTTONS)}

            # Read persisted ranks explicitly; the small list keeps this deterministic.

            ranked=[]

            for name in BUTTON_GROUPS[button_group(key)][1]:

                raw=await self.repo.setting('button_position:'+name,str(default[name]))

                ranked.append((int(raw) if raw.lstrip('-').isdigit() else default[name],default[name],name))

            order=[name for _,__,name in sorted(ranked)]

            index=order.index(key);other=index-1 if direction=='up' else index+1

            if 0<=other<len(order): order[index],order[other]=order[other],order[index]

            for position,name in enumerate(order): await self.repo.set_setting('button_position:'+name,str(position))

            await self.audit(actor,'button_move',key,direction)

            return await self._render(f'admin:button:{key}',actor)

        if page=='buttonstyle':

            key,style=p[2],p[3]

            if key not in EDITABLE_BUTTONS or style not in ('default','primary','success','danger'):

                raise ValueError('Настройка недоступна.')

            old=await self.repo.setting('button_style:'+key)

            await self.repo.set_setting('button_style:'+key,'' if style=='default' else style)

            await self.audit(actor,'button_style',key,f'{old or "default"} → {style}')

            return await self._render(f'admin:button:{key}',actor)

        if page == 'texts':

            welcome=await self.repo.setting('welcome','Оставь границы позади.')

            return Screen(f'👋 <b>Приветственное сообщение</b>\n\nТекущий текст:\n{escape(welcome)}\n\nПриветствие — текст над главным меню. Название сервиса заменяет бренд в сообщениях. Ссылка поддержки используется для контакта. Переносы строк сохраняются.', [[('✏️ Изменить приветствие', 'admin:input:text:welcome')], [('Название сервиса', 'admin:input:text:brand')], [('Ссылка поддержки', 'admin:input:text:support_url')], back('admin:x:appearance')])

        if page == 'audit':

            offset = max(0, int(p[2]))

            entries = list(await self.session.scalars(select(AdminAudit).order_by(AdminAudit.id.desc()).offset(offset).limit(6)))

            text = '\n'.join(f'#{a.id} · {a.created_at:%d.%m %H:%M} · {a.actor_telegram_id}\n{escape(a.action)} · {escape(a.target)}\n{escape(a.details[:120])}' for a in entries[:5]) or 'Действий пока нет.'

            rows = []

            if offset:

                rows.append([('← Предыдущие', f'admin:audit:{max(0, offset-5)}')])

            if len(entries) > 5:

                rows.append([('Следующие →', f'admin:audit:{offset+5}')])

            return Screen('📋 <b>Журнал действий</b>\n\n' + text, rows + [back('admin:more')])

        if page in ('confirm', 'do'):

            operation = p[2]

            target = p[3] if operation in ('tarifftoggle','tariffdelete') and p[3].startswith('id') else int(p[3])

            allowed = {'block', 'keydelete', 'tarifftoggle', 'tariffdelete', 'trialtoggle', 'referraltoggle'}

            if operation not in allowed:

                raise ValueError('Неизвестное действие.')

            if page == 'confirm':

                token = secrets.token_hex(6)

                await self.repo.set_setting('confirm:' + token, json.dumps({'actor': actor.telegram_id, 'operation': operation, 'target': target, 'expires': now().timestamp() + 300}))

                names = {'block': 'Изменить блокировку клиента', 'keydelete': 'Удалить ключ (без восстановления)', 'tariffdelete':'Удалить тариф из продажи (действующие подписки сохранятся)', 'tarifftoggle': 'Изменить доступность тарифа', 'trialtoggle': 'Изменить доступность пробного периода', 'referraltoggle': 'Изменить доступность рефералов'}

                return Screen(f'{names[operation]}?\nОбъект: #{target}', [[('✅ Подтвердить', f'admin:do:{operation}:{target}:{token}')], back('admin:home')])

            token = p[4] if len(p) == 5 else ''

            stored = await self.repo.setting('confirm:' + token)

            if not stored:

                raise ValueError('Подтверждение уже использовано или недоступно.')

            confirmation = json.loads(stored)

            if confirmation['actor'] != actor.telegram_id or confirmation['operation'] != operation or confirmation['target'] != target or confirmation['expires'] < now().timestamp():

                raise ValueError('Подтверждение истекло или недоступно.')

            from app.database.models import AppSetting

            # Lock confirmation row before consuming it, including concurrent clicks.

            claimed = await self.session.execute(sql_delete(AppSetting).where(AppSetting.key == 'confirm:' + token, AppSetting.value == stored))

            if claimed.rowcount != 1:

                raise ValueError('Подтверждение уже использовано.')

            if operation == 'block':

                u = await self.customer(target)

                if self.settings.is_admin(u.telegram_id):

                    raise ValueError('Блокировка администратора запрещена.')

                u.status = 'active' if u.status == 'blocked' else 'blocked'

                result, destination = u.status, f'admin:client:{u.id}'

            elif operation == 'tariffdelete':

                t=await self.repo.tariff(target)

                if not t:raise ValueError('Тариф не найден.')

                from app.services.catalog_lifecycle import retire

                meta=json.loads(await self.repo.setting(f'catalog_vpn:{t.id}','{}'))

                retired=await retire(self.repo,'vpn',t.id,meta.get('version',0))

                result,destination='deleted' if retired['deleted'] else 'archived','admin:tariffs'

            elif operation == 'keydelete':

                key = await self.session.get(AccessKey, target)

                if not key or key.status == 'deleted':

                    raise ValueError('Ключ не найден или удалён.')

                key.status = 'deleted'

                await self.repo.set_setting(f'client_frozen:{key.id}','0')

                await self.repo.set_setting(f'client_freeze_until:{key.id}','')

                result, destination = key.status, f'admin:keys:{key.user_id}'

            else:

                name, destination = {'tarifftoggle': (f'tariff_enabled:{target}', f'admin:tariff:{target}'), 'trialtoggle': ('trial_enabled', 'admin:trial'), 'referraltoggle': ('referral_enabled', 'admin:referrals')}[operation]

                if operation == 'tarifftoggle':

                    tariff=await self.repo.tariff(target)

                    if not tariff:raise ValueError('Тариф не найден.')

                    name=f'tariff_enabled:id{tariff.id}'

                current=await self.repo.tariff_setting(tariff,'tariff_enabled','1') if operation=='tarifftoggle' else await self.repo.setting(name,'1')

                result = '0' if current == '1' else '1'

                await self.repo.set_setting(name, result)

            await self.audit(actor, operation, target, result)

            if operation == 'trialtoggle':

                screen = await self._render('admin:trial', actor)

                screen.text = '✅ Настройка применена.\n\n' + screen.text

                return screen

            return Screen('✅ Изменение сохранено.', [back(destination)])

        if page == 'input' and p[2]=='referral_tiers':

            return Screen('Введите через пробел 8 чисел:\n\nНачало второй ступени, начало третьей, затем проценты первой/повторной покупки для трёх ступеней.\n\nПример: 6 16 15 5 20 10 30 15', [back('admin:referrals')],structured=True)

        if page == 'input':

            kind = p[2]

            if kind=='antig_custom':
                from app.services.bot_antig_addon import Addon
                screen=await Addon(self.repo,self.settings).render(f'ag:price:{p[3]}',actor)
                if len(p)>4:
                    screen.rows=[[('← Ключ',f'admin:r:key:{int(p[4])}')]]
                    screen.admin_input_action=action
                return screen

            if kind in ('stars_price', 'stars_antig_price'):
                tariff = await self.repo.tariff(p[3])
                if not tariff:
                    raise ValueError('Тариф недоступен.')
                minimum = 0 if kind == 'stars_antig_price' else 1
                return Screen(f'Введите цену от {minimum} до 10 000 ⭐ или «авто» для пересчёта из рублей.',
                              [back(f'admin:stars:tariff:{tariff.id}')])

            if kind=='antig_price':
                from app.services.bot_antig_price import AntigPrice
                return await AntigPrice(self.repo,self.settings).prompt(actor,int(p[3]) if len(p)>3 else None)

            if kind=='giftlink':
                from app.services.bot_gift_links import BotGiftLinks
                return await BotGiftLinks(self.repo,self.settings).prompt(p[3],actor)
            if kind=='agfee':
                from app.services.bot_admin_renew import BotAdminRenew
                return await BotAdminRenew(self.repo,self.settings).fee_prompt(p[3],actor)
            if kind=='renewcustom':
                from app.services.bot_admin_renew import BotAdminRenew
                return await BotAdminRenew(self.repo,self.settings).prompt(int(p[3]),int(p[4]),actor)
            if kind=='proxygrant':

                from app.services.bot_proxy_grant import BotProxyGrant

                return await BotProxyGrant(self.repo,self.settings).prompt(p[3],p[4],actor)

            if kind=='grantconfig':

                from app.services.bot_grant_config import BotGrantConfig

                return await BotGrantConfig(self.repo,self.settings).prompt(p[3],p[4],actor)

            if kind=='clientref':

                from app.services.personal_referrals import PersonalReferrals

                return await PersonalReferrals(self.repo,self.settings).prompt(int(p[3]),actor)

            if kind=='clientgrant':

                from app.services.admin_client_grant import ClientGrant

                return await ClientGrant(self.repo,self.settings).prompt(int(p[3]),actor)

            if kind == 'extend':

                from app.database.models import VlessCredential

                credential = await self.session.scalar(select(VlessCredential).where(VlessCredential.subscription_id == int(p[3])))

                if credential:

                    await self.repo.set_setting(f'vless_extend_intent:{actor.id}:{credential.id}', secrets.token_hex(16))

            prompts = {'tariff_add':'Введите: название | срок в месяцах | цена в рублях | устройства\nНапример: Семейная подписка | 2 | 500 | 3\nСрок: 1–36 месяцев, устройства: 1–100. Можно создавать несколько тарифов с одинаковым сроком.', 'search': 'Введите внутренний ID, Telegram ID, Freeman ID, email, логин сайта, Telegram username или имя:', 'key': 'Введите название и срок в днях через |\nНапример: iPhone | 30\nСоздаётся только тестовая запись.', 'extend': 'Введите количество дней для продления (1–3650):', 'tariff_name': 'Введите новое название тарифа (до 80 символов):', 'tariff_price': 'Введите цену в рублях, например 400 или 399.50:', 'tariff_limit': 'Введите лимит устройств (1–100):', 'stars_price': 'Цена Stars теперь рассчитывается автоматически из цены тарифа в рублях.', 'button': 'Введите новое название кнопки (до 64 символов):', 'promo': 'Введите КОД | скидка в процентах\nНапример: SUMMER15 | 15', 'promo_toggle': 'Введите код для включения / выключения:', 'server': 'Введите название | адрес\nНапример: Финляндия | vpn.example.com\nЭто реестр, подключение и проверка доступности не выполняются.'}

            if kind == 'custom_button':

                prompt=('Введите: Название | экран | переход\n\nЭкраны: '+', '.join(CUSTOM_BUTTON_LOCATIONS)+'\nПереходы: '+', '.join(sorted(CUSTOM_BUTTON_TARGETS))+'\nИли публичная ссылка https://...\n\nПример: Новости | home | https://t.me/example')

            elif kind.startswith('custom_button_'):

                await self.custom_button(p[3]);field=kind.removeprefix('custom_button_')

                custom_prompts={

                    'label':'Введите новое название кнопки (до 64 символов):',

                    'page':'Введите экран: '+', '.join(CUSTOM_BUTTON_LOCATIONS),

                    'target':'Введите раздел бота ('+', '.join(sorted(CUSTOM_BUTTON_TARGETS))+') или публичную HTTPS-ссылку:',

                    'position':'Введите позицию от 0 до 9999. Меньшее число поднимает кнопку выше:',

                }

                if field not in custom_prompts: raise ValueError('Поле недоступно.')

                prompt=custom_prompts[field]

            elif kind == 'key_id':

                await self.customer(p[3])

                prompt = 'Введите ID существующего ключа (номер после #):'

            elif kind=='balance_credit':

                await self.customer(p[3])

                prompt='Введите сумму в рублях (до 2 знаков после запятой). Подтверждайте пополнение только после проверки оплаты или для выдачи бонуса.'

            elif kind in ('client_discount','client_price'):

                await self.customer(p[3])

                prompt='Введите постоянную скидку в процентах (0–100). 0 отменяет скидку.' if kind=='client_discount' else 'Введите индивидуальную цену в рублях (0–1 000 000, до 2 знаков после запятой). Введите - для возврата обычной цены.'

            elif kind == 'setting':

                prompt = ('Срок пробной подписки Telegram в часах (72 = 3 дня)' if p[3]=='trial_hours' else SETTING_FIELDS[p[3]][0]) + ':'

            elif kind == 'text':

                defaults={'welcome':'Оставь границы позади.','brand':'F R E E M A N','support_url':''}

                current=await self.repo.setting(p[3],defaults.get(p[3],''))

                prompt = 'Введите новое приветственное сообщение (до 2000 символов). Оно показывается над главным меню:' if p[3] == 'welcome' else 'Введите новое название (до 80 символов):' if p[3] == 'brand' else 'Введите https-ссылку поддержки:'

                prompt += '\n\nТекущий текст (нажмите, чтобы скопировать):\n<code>'+escape(current)+'</code>\n\nСкопируйте текст, поправьте его и отправьте новым сообщением. Переносы строк сохраняются.'

            elif kind=='staff':

                if not self.settings.is_owner(actor.telegram_id):

                    raise PermissionError('Только владелец управляет ролями.')

                prompt='Введите Telegram ID сотрудника. Далее выберите доступ кнопками. Сотрудник должен предварительно запустить бота.'

            elif kind=='button_icon':

                if p[3] not in EDITABLE_BUTTONS:

                    raise ValueError('Кнопка недоступна.')

                prompt='Отправьте один премиум-эмодзи Telegram или его custom_emoji_id. Обычный стикер не подходит. Кастомный эмодзи появится перед текстом вместо обычного смайлика. Для удаления отправьте -.'

            elif kind == 'payment_yookassa':

                prompt = ('🔐 Отправьте одним сообщением:\nSHOP_ID | SECRET_KEY | режим\n\n'

                          'Режим: live или test. Пример: 123456 | test_... | live\n'

                          'Сообщение будет удалено сразу после обработки, а реквизиты сохранятся зашифрованными.')

            elif kind == 'payment_robokassa':

                prompt = ('🔐 Отправьте одним сообщением:\nMERCHANT_LOGIN | PASSWORD1 | PASSWORD2 | HTTPS_RESULT_URL\n\n'

                          'Сообщение будет удалено сразу после обработки, а реквизиты сохранятся зашифрованными.')

            elif kind == 'protocol_price':

                prompt = 'Введите цену этого продукта в рублях, например 499 или 499.50:'

            elif kind == 'protocol_device_limit':

                prompt = 'Введите максимальное количество устройств от 1 до 100:'

            elif kind == 'protocol_app':

                prompt = 'Введите название основного приложения, например Happ:'

            elif kind == 'protocol_description':

                prompt = 'Введите описание продукта для клиента, до 1000 символов:'

            elif kind == 'protocol_instruction':

                prompt = 'Введите краткую инструкцию по установке, до 2000 символов:'

            elif kind == 'protocol_instruction_app':

                app_names = {'happ': 'Happ', 'v2raytun': 'v2RayTun', 'incy': 'Incy'}

                if len(p) < 5 or p[3] != 'vless' or p[4] not in app_names:

                    raise ValueError('Приложение недоступно.')

                prompt = f'Введите инструкцию VLESS для {app_names[p[4]]}, до 2000 символов:'

            else:

                prompt = prompts[kind]

            destination=(f'admin:custombutton:{p[3]}' if kind.startswith('custom_button_') else 'admin:custombuttons' if kind=='custom_button' else f'admin:issue:{p[3]}' if kind in ('key', 'key_id') else 'admin:promos' if kind in ('promo','promo_toggle') else 'admin:referrals' if kind=='setting' and len(p)>3 and p[3].startswith('referral_') else 'admin:home')

            if kind=='search':destination='home'

            elif kind=='staff':destination='admin:roles'

            elif kind=='text':destination='admin:texts'

            elif kind in ('button','button_icon'):destination=f'admin:button:{p[3]}'

            return Screen(prompt, [back(destination)])

        raise ValueError('Раздел недоступен.')



    async def apply_input(self, action, value, actor):

        from app.services.admin_key_navigation import split,search_context

        base,origin=split(action)

        screen=await self.apply_input_core(base,value,actor)
        if base.startswith('admin:input:giftlink:'):
            from app.services.screens import ScreenService
            return await ScreenService(self.repo,self.settings,None,None).decorate('gift_links',actor,screen)
        if base.startswith(('admin:input:renewcustom:','admin:input:agfee:','admin:input:antig_custom:','admin:input:saleprice:')):
            if screen.admin_input_action and origin:
                screen.admin_input_action += '~'+origin
            from app.services.admin_key_navigation import decorate
            return decorate(screen,origin,base)
        return search_context(screen,origin,base)



    async def apply_input_core(self, action, value, actor):

        self.require(actor)

        if not self.settings.can_admin_action(actor.telegram_id, action):

            raise PermissionError('Недостаточно прав для этого действия.')

        p = action.split(':')

        if p[:2] != ['admin', 'input']:

            raise ValueError('Неверное действие.')

        kind = p[2]

        if kind == 'public_proxy':
            if not self.settings.is_owner(actor.telegram_id):
                raise PermissionError('Изменение ссылки доступно администратору.')
            from app.services.public_proxy import replace
            return await replace(self.repo, self.settings, actor, value)

        if kind == 'discount':
            from app.services.timed_discount import configure
            return await configure(self.repo, self.settings, actor, p[3], value)

        if kind=='proxycampaign':
            from app.services.proxy_campaigns import ProxyCampaigns
            return await ProxyCampaigns(self.repo,self.settings).input(value,actor)

        if kind=='partnerdebt':
            from app.services.partner_ledger import PartnerLedger
            return await PartnerLedger(self.repo,self.settings).input(action,value,actor)

        if kind=='antig_custom':
            from app.services.bot_antig_addon import Addon
            screen=await Addon(self.repo,self.settings).set_price(actor,int(p[3]),value)
            if len(p)>4:
                screen.rows=[[('← Ключ',f'admin:r:key:{int(p[4])}')]]
            return screen

        if kind=='antig_price':
            from app.services.bot_antig_price import AntigPrice
            return await AntigPrice(self.repo,self.settings).input(value,actor,int(p[3]) if len(p)>3 else None)

        if kind=='saleprice':
            from app.services.bot_sale_price import SalePrice
            return await SalePrice(self.repo,self.settings).input(int(p[3]),value,actor)

        if kind=='giftlink':
            from app.services.bot_gift_links import BotGiftLinks
            return await BotGiftLinks(self.repo,self.settings).input(p[3],value,actor)
        if kind=='agfee':
            from app.services.bot_admin_renew import BotAdminRenew
            return await BotAdminRenew(self.repo,self.settings).fee_input(p[3],value,actor)
        if kind=='renewcustom':
            from app.services.bot_admin_renew import BotAdminRenew
            return await BotAdminRenew(self.repo,self.settings).input(int(p[3]),int(p[4]),value,actor)
        if kind=='proxygrant':

            from app.services.bot_proxy_grant import BotProxyGrant

            return await BotProxyGrant(self.repo,self.settings).input(p[3],p[4],value,actor)

        if kind=='grantconfig':

            from app.services.bot_grant_config import BotGrantConfig

            return await BotGrantConfig(self.repo,self.settings).input(p[3],p[4],value,actor)

        if kind=='clientref':

            from app.services.personal_referrals import PersonalReferrals

            return await PersonalReferrals(self.repo,self.settings).input(int(p[3]),value,actor)

        if kind=='clientgrant':

            from app.services.admin_client_grant import ClientGrant

            return await ClientGrant(self.repo,self.settings).custom(int(p[3]),value,actor)

        if kind=='referral_tiers':

            try: values=[int(v) for v in value.split()]

            except ValueError:raise ValueError('Введите 8 целых чисел через пробел.')

            if len(values)!=8 or not 2<=values[0]<values[1]<=100000 or any(not 0<=v<=100 for v in values[2:]):raise ValueError('Пороги: 2–100000, второй меньше третьего; проценты: 0–100.')

            await self.repo.set_setting('referral_tiers',json.dumps(values))

            await self.audit(actor,'referral_tiers',0,values)

            return await self._render('admin:referrals',actor)

        value = value.strip()

        if not value or len(value) > 2000:

            raise ValueError('Введите от 1 до 2000 символов.')

        target = p[3] if len(p) > 3 else ''

        destination = 'admin:home'

        if kind=='custom_button':

            parts=[part.strip() for part in value.split('|')]

            if len(parts)!=3: raise ValueError('Формат: Название | экран | переход.')

            label,page_name,button_target=parts;self.length(label,64)

            if page_name not in CUSTOM_BUTTON_LOCATIONS: raise ValueError('Неизвестный экран размещения.')

            button_target=self.validate_custom_target(button_target);token=secrets.token_hex(4)

            item={'label':label,'page':page_name,'target':button_target,'position':1000,'visible':True}

            await self.repo.set_setting('custom_button:'+token,json.dumps(item,ensure_ascii=False));await self.audit(actor,'custom_button_create',token,item)

            return Screen('✅ Кнопка создана и уже доступна клиентам.',[back(f'admin:custombutton:{token}')])

        if kind.startswith('custom_button_'):

            token=target;item=await self.custom_button(token);field=kind.removeprefix('custom_button_');old=dict(item)

            if field=='label': self.length(value,64);item['label']=value

            elif field=='page':

                if value not in CUSTOM_BUTTON_LOCATIONS: raise ValueError('Неизвестный экран размещения.')

                item['page']=value

            elif field=='target': item['target']=self.validate_custom_target(value)

            elif field=='position': item['position']=self.integer(value,0,9999)

            else: raise ValueError('Поле недоступно.')

            await self.repo.set_setting('custom_button:'+token,json.dumps(item,ensure_ascii=False));await self.audit(actor,'custom_button_edit',token,f'{old} → {item}')

            return Screen('✅ Настройка кнопки сохранена.',[back(f'admin:custombutton:{token}')])

        if kind == 'search':

            from app.services.bot_admin_search import BotAdminSearch

            return await BotAdminSearch(self.repo,self.settings).find(value,actor)

        if kind=='staff':

            if not self.settings.is_owner(actor.telegram_id):

                raise PermissionError('Только владелец управляет ролями.')

            raw_id=value.strip()

            if not raw_id.isdigit() or int(raw_id)<=0 or int(raw_id) in self.settings.owner_ids():raise ValueError('Укажите Telegram ID сотрудника. Владельцу доступ не меняется.')

            telegram_id=int(raw_id)

            await self.repo.set_setting(f'staff_role:{telegram_id}',await self.repo.setting(f'staff_role:{telegram_id}') or 'support')

            await self.repo.set_setting(f'staff_blocked:{telegram_id}','1')

            self.settings.set_staff_role(telegram_id,None)

            await self.audit(actor,'staff_add',telegram_id,'Ожидает выбора и включения доступа')

            return await self.render(f'admin:staff:{telegram_id}',actor)

        if kind in ('payment_yookassa', 'payment_robokassa'):

            from app.services.payment_settings import PaymentSettings

            values = [part.strip() for part in value.split('|')]

            service = PaymentSettings(self.repo, self.settings)

            if kind == 'payment_yookassa':

                if len(values) != 3 or not values[0] or len(values[1]) < 8 or values[2].lower() not in ('live', 'test'):

                    raise ValueError('Формат: SHOP_ID | SECRET_KEY | live или test.')

                await service.save(dict(yookassa_shop_id=values[0], yookassa_secret_key=values[1], yookassa_test_mode=values[2].lower() == 'test'))

                await self.repo.set_setting('payment_provider:yookassa', '1')

                await self.audit(actor, 'payment_settings', 'yookassa', 'Реквизиты обновлены через защищённый ввод')

                return Screen('✅ Реквизиты ЮKassa зашифрованы и сохранены. Выполните проверку API.', [[('🔄 Проверить API', 'admin:yookassatest')], back('admin:yookassa')])

            from urllib.parse import urlparse

            if len(values) != 4 or not all(values[:3]) or len(values[1]) < 8 or len(values[2]) < 8:

                raise ValueError('Формат: MERCHANT_LOGIN | PASSWORD1 | PASSWORD2 | HTTPS_RESULT_URL.')

            parsed = urlparse(values[3])

            if parsed.scheme != 'https' or not parsed.hostname:

                raise ValueError('RESULT_URL должен быть публичным HTTPS-адресом.')

            await service.save(dict(robokassa_merchant_login=values[0], robokassa_password1=values[1], robokassa_password2=values[2], robokassa_result_url=values[3]))

            await self.audit(actor, 'payment_settings', 'robokassa', 'Реквизиты обновлены через защищённый ввод')

            return Screen('✅ Реквизиты Robokassa зашифрованы и сохранены. Включение внешнего checkout станет доступно после подключения обработчика ResultURL.', [back('admin:robokassa')])

        if kind.startswith('protocol_'):

            protocol = target

            if protocol not in ('amnezia', 'vless'):

                raise ValueError('Протокол недоступен.')

            if kind == 'protocol_price':

                tariff = await self.repo.tariff(p[4])

                if not tariff:

                    raise ValueError('Тариф недоступен.')

                try:

                    price = Decimal(value.replace(',', '.'))

                    if not price.is_finite() or price <= 0 or price > 1_000_000 or price * 100 != (price * 100).to_integral_value():

                        raise ValueError('Цена: 0.01–1 000 000 ₽, до двух знаков после запятой.')

                except InvalidOperation:

                    raise ValueError('Введите цену числом.')

                key = f'protocol_price:{protocol}:id{tariff.id}'; result = str(int(price * 100))

            elif kind == 'protocol_device_limit':

                key = f'protocol_device_limit:{protocol}'; result = str(self.integer(value, 1, 100))

            elif kind == 'protocol_app':

                self.length(value, 80); key = f'protocol_app:{protocol}'; result = value

            elif kind == 'protocol_description':

                self.length(value, 1000); key = f'protocol_description:{protocol}'; result = value

            elif kind == 'protocol_instruction':

                self.length(value, 2000); key = f'protocol_instruction:{protocol}'; result = value

            elif kind == 'protocol_instruction_app':

                apps = {'happ', 'v2raytun', 'incy'}

                if protocol != 'vless' or len(p) < 5 or p[4] not in apps:

                    raise ValueError('Приложение недоступно.')

                self.length(value, 2000); key = f'protocol_instruction:vless:{p[4]}'; result = value

            else:

                raise ValueError('Поле протокола недоступно.')

            old = await self.repo.setting(key)

            await self.repo.set_setting(key, result)

            await self.audit(actor, kind, protocol, f'{key}: {old} → {result[:120]}')

            return Screen('✅ Настройка продукта сохранена.', [back(f'admin:protocol:{protocol}')])

        if kind == 'key_id':

            key_id = self.integer(value.lstrip('#'), 1, 2**31 - 1)

            return await self.assignment_preview(key_id, int(target), actor)

        if kind=='balance_credit':

            u=await self.customer(target)

            try:

                amount=Decimal(value.replace(',','.'))*100

                if not amount.is_finite() or amount!=amount.to_integral_value() or not 0<amount<=100000000:

                    raise ValueError('Введите сумму от 0.01 до 1 000 000 ₽ с точностью до копейки.')

            except InvalidOperation:

                raise ValueError('Неверная сумма.')

            token=secrets.token_hex(8)

            await self.repo.set_setting('balance_credit:'+token,json.dumps(dict(actor=actor.telegram_id,user=u.id,amount=int(amount),expires=now().timestamp()+300)))

            return Screen(f'Пополнить баланс клиента {u.telegram_id} на {money(int(amount))}?\nЭто ручное пополнение, платёж провайдера не создаётся.',[[('✅ Подтвердить',f'admin:credit:{token}')],back(f'admin:client:{u.id}')])

        if kind in ('client_discount','client_price'):

            u=await self.customer(target)

            key=f'client_discount:{u.id}'

            if kind=='client_discount':

                result=str(self.integer(value,0,100))

            else:

                tariff=await self.repo.tariff(p[4])

                if not tariff:

                    raise ValueError('Тариф недоступен.')

                key=f'client_price:{u.id}:id{tariff.id}'

                if value=='-':

                    result=''

                else:

                    try:

                        price=Decimal(value.replace(',','.'))

                        if not price.is_finite() or price<0 or price>1000000 or price*100!=(price*100).to_integral_value():

                            raise ValueError('Цена: 0–1 000 000 ₽, до 2 знаков после запятой.')

                        result=str(int(price*100))

                    except InvalidOperation:

                        raise ValueError('Введите цену числом.')

            old=await self.repo.setting(key)

            await self.repo.set_setting(key,result)

            details=f'{key}: {old} → {result}';destination=f'admin:clientprices:{u.id}'

        elif kind == 'setting':

            label, minimum, maximum, default = SETTING_FIELDS[target]

            number = self.integer(value, minimum, maximum)

            setting = 'bot_trial_hours' if target == 'trial_hours' else target
            old = await self.repo.setting(setting, '72' if target == 'trial_hours' else default)

            await self.repo.set_setting(setting, number)

            details = f'{old} → {number}'

            destination = 'admin:trial' if target == 'trial_hours' else 'admin:referrals' if target.startswith('referral_') else 'admin:more' if target == 'device_limit' else 'admin:home'

        elif kind in ('stars_price', 'stars_antig_price'):
            from app.services.bot_catalog import available
            tariff = await self.repo.tariff(target)
            if not tariff or all(t.id != tariff.id for t in await available(self.repo)):
                raise ValueError('Тариф недоступен.')
            addon = kind == 'stars_antig_price'
            key = 'stars_antig_override' if addon else 'stars_tariff_override'
            current = await self.repo.tariff_setting(tariff, key, '')
            new = '' if value.strip().casefold() in ('авто', 'auto') else str(self.integer(value, 0 if addon else 1, 10_000))
            await self.repo.set_tariff_setting(tariff, key, new)
            details = f'{current or "авто"} → {new or "авто"}'
            destination = f'admin:stars:tariff:{tariff.id}'

        elif kind=='tariff_add':

            fields=[x.strip() for x in value.split('|')]

            if len(fields)!=4:raise ValueError('Формат: название | месяцы | цена в рублях | устройства')

            name,months,price,devices=fields;self.length(name,80)

            months=self.integer(months,1,36);devices=self.integer(devices,1,100)

            try:

                amount=Decimal(price.replace(',','.'))*100

                if not amount.is_finite() or not 1<=amount<=100000000 or amount!=amount.to_integral_value():raise ValueError('Некорректная цена.')

            except InvalidOperation:raise ValueError('Введите цену числом.')

            t=await self.repo.add(Tariff(months=months,price=int(amount),device_limit=devices))

            t.price=int(amount);t.device_limit=devices

            await self.repo.set_tariff_setting(t,'tariff_name',name)

            await self.repo.set_tariff_setting(t,'tariff_enabled','1')

            await self.repo.set_tariff_setting(t,'tariff_deleted','0')

            meta=json.loads(await self.repo.setting(f'catalog_vpn:{t.id}','{}'))

            meta.update(archived=False,version=meta.get('version',0)+1)

            await self.repo.set_setting(f'catalog_vpn:{t.id}',json.dumps(meta))

            details,destination=f'{name}; {months} мес.; {devices} устройств',f'admin:tariff:id{t.id}'

        elif kind.startswith('tariff_'):

            t = await self.repo.tariff(target)

            if not t:

                raise ValueError('Тариф не найден.')

            if kind == 'tariff_name':

                self.length(value, 80)

                old = await self.label(t)

                await self.repo.set_tariff_setting(t,'tariff_name',value)

            elif kind == 'tariff_price':

                old = money(t.price)

                try:

                    price = Decimal(value.replace(',', '.'))

                    if not price.is_finite() or price <= 0 or price > 1000000 or price * 100 != (price * 100).to_integral_value():

                        raise ValueError('Цена: от 0.01 до 1 000 000 ₽, не более 2 знаков после запятой.')

                    t.price = int(price * 100)

                except InvalidOperation:

                    raise ValueError('Введите цену числом.')

            elif kind == 'tariff_limit':

                old = t.device_limit

                t.device_limit = self.integer(value, 1, 100)

            else:

                raise ValueError('Неизвестное поле тарифа.')

            details, destination = f'{old} → {value}', f'admin:tariff:{target}'

        elif kind == 'button':

            if target not in EDITABLE_BUTTONS:

                raise ValueError('Кнопка недоступна для редактирования.')

            self.length(value, 64)

            old = await self.repo.setting('button:' + target, EDITABLE_BUTTONS[target])

            await self.repo.set_setting('button:' + target, value)

            details, destination = f'{old} → {value}', f'admin:button:{target}'

        elif kind=='button_icon':

            if target not in EDITABLE_BUTTONS:

                raise ValueError('Кнопка недоступна для редактирования.')

            if value!='-' and (not value.isdigit() or not 10<=len(value)<=30):

                raise ValueError('Отправьте один кастомный эмодзи, его числовой ID или - для удаления.')

            old=await self.repo.setting('button_icon:'+target)

            new='' if value=='-' else value

            await self.repo.set_setting('button_icon:'+target,new)

            details,destination=f'{old or "нет"} → {new or "нет"}',f'admin:button:{target}'

        elif kind == 'text':

            if target not in ('welcome', 'brand', 'support_url'):

                raise ValueError('Неизвестное поле.')

            self.length(value, 2000 if target == 'welcome' else 80 if target == 'brand' else 500)

            if target == 'support_url':

                from urllib.parse import urlparse

                parsed = urlparse(value)

                if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:

                    raise ValueError('Укажите корректную https-ссылку без пароля.')

            old = await self.repo.setting(target, '')

            await self.repo.set_setting(target, value)

            details, destination = f'{old} → {value}', 'admin:texts'

        elif kind == 'key':

            u = await self.customer(target)

            if u.status == 'blocked':

                raise ValueError('Сначала разблокируйте клиента.')

            name, separator, days = value.partition('|')

            if not separator:

                raise ValueError('Формат: название | срок в днях.')

            self.length(name.strip(), 100)

            days = self.integer(days.strip(), 1, 3650)

            key = AccessKey(user_id=u.id, name=name.strip(), expires_at=now() + timedelta(days=days))

            await self.repo.add(key)

            details, destination, target = f'Тестовый ключ: {name.strip()}, {days} дней', f'admin:keys:{u.id}', str(key.id)

        elif kind == 'extend':

            s = await self.session.get(Subscription, int(target))

            if not s:

                raise ValueError('Подписка не найдена.')

            from datetime import timezone

            days = self.integer(value, 1, 3650)

            from app.database.models import VlessCredential

            credential = await self.session.scalar(select(VlessCredential).where(VlessCredential.subscription_id == s.id))

            if credential:

                from app.services.offers import lock_customer

                from app.services.vless_renewal import locked_context

                from app.services.vless_provision import control

                await lock_customer(self.repo, s.user_id)

                s, key, credential = await locked_context(self.repo, credential)

                token = await self.repo.setting(f'vless_extend_intent:{actor.id}:{credential.id}')

                if not token:raise ValueError('Откройте продление заново для подтверждения заказа.')

                expiry = max(now(), s.expires_at.replace(tzinfo=timezone.utc)) + timedelta(days=days)

                await control(self.repo,self.settings,credential,'extend',expiry,renewal=dict(operation_key='bot-extend:'+token,request=dict(days=days),devices=credential.device_limit,activate=key.status=='active'))

            else:

                expiry = s.expires_at.replace(tzinfo=timezone.utc) if s.expires_at.tzinfo is None else s.expires_at

                s.expires_at = max(now(), expiry) + timedelta(days=days)

            details, destination = f'+{days} дней → {s.expires_at.isoformat()}', f'admin:subscription:{s.id}'

        elif kind in ('promo', 'promo_toggle'):

            import re

            code, _, amount = value.upper().partition('|')

            code = code.strip()

            if not re.fullmatch(r'[A-Z0-9_]{1,32}', code):

                raise ValueError('Код: 1–32 латинских буквы, цифры или _.')

            promo = await self.repo.promo(code)

            if kind == 'promo':

                number = self.integer(amount.strip(), 1, 100)

                if promo:

                    promo.value = number

                else:

                    await self.repo.add(PromoCode(code=code, effect='percent_discount', value=number))

                details = f'{code}: {number}%'

            else:

                if not promo:

                    raise ValueError('Промокод не найден.')

                enabled = '0' if await self.repo.setting('promo_enabled:' + code, '1') == '1' else '1'

                await self.repo.set_setting('promo_enabled:' + code, enabled)

                details = f'{code}: enabled={enabled}'

            destination, target = 'admin:promos', code

        elif kind == 'server':

            name, separator, address = value.partition('|')

            if not separator:

                raise ValueError('Формат: название | адрес.')

            self.length(name.strip(), 100)

            self.length(address.strip(), 255)

            server = Server(name=name.strip(), address=address.strip())

            await self.repo.add(server)

            details, destination, target = value, 'admin:servers', str(server.id)

        else:

            raise ValueError('Действие недоступно.')

        await self.audit(actor, kind, target, details)

        if kind == 'key':

            return Screen('✅ Новый тестовый ключ создан и назначен клиенту.\nОн увидит запись в «Мои подписки» → «Мои ключи».\nРеальное подключение пока не создано.', [back(destination)])

        return Screen('✅ Сохранено.', [back(destination)])



    @staticmethod

    def integer(value, minimum, maximum):

        try:

            number = int(value)

        except (TypeError, ValueError):

            raise ValueError('Введите целое число.')

        if not minimum <= number <= maximum:

            raise ValueError(f'Допустимо от {minimum} до {maximum}.')

        return number



    @staticmethod

    def length(value, limit):

        if not value or len(value) > limit or any(ord(c) < 32 and c != '\n' for c in value):

            raise ValueError(f'Введите от 1 до {limit} символов без управляющих знаков.')

