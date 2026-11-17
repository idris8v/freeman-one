from app.services.subscription_labels import expiry_date
from dataclasses import dataclass, field
import json,re,hashlib
from datetime import timezone
from math import ceil
from urllib.parse import urlencode
from html import escape
from app.bot.keyboards import back
from app.config.content import FAQ, DOCUMENTS, DOCUMENT_TEXTS
from app.config.admin_content import VLESS_INSTRUCTIONS as INSTRUCTIONS
from app.database.models import now
from app.services.domain import SubscriptionService, PromoService
from app.services.accounts import display_freeman_id


@dataclass
class Screen:
    text: str
    rows: list
    photo: str | tuple[bytes,str] | None = None
    animation: str | None = None
    video: str | None = None
    document: tuple[bytes, str] | str | None = None
    clear_keyboard: bool = False
    button_options: dict = field(default_factory=dict)
    keep_input: bool = False
    rich: object | None = None
    appearance_page: str | None = None
    banner: tuple[str,str] | None = None
    appearance_applied: bool = False
    native_text: bool = False
    structured: bool = False
    gift_target: str | None = None
    avatar_telegram_id: int | None = None
    admin_input_action: str | None = None


def button_key(target):
    from app.config.admin_content import stable_button_key
    return stable_button_key(target)

def money(kopecks):
    value = f'{kopecks / 100:,.2f}'.replace(',', ' ').rstrip('0').rstrip('.')
    return value + ' ₽'


def duration(months):
    return f'{months} ' + ('месяц' if months%10==1 and months%100!=11 else 'месяца' if months%10 in (2,3,4) and months%100 not in (12,13,14) else 'месяцев')


def time_remaining(expiry):
    seconds=(expiry.replace(tzinfo=timezone.utc)-now()).total_seconds()
    return 'Истёк' if seconds<=0 else f'{ceil(seconds/3600)} ч.' if seconds<=86400 else f'{ceil(seconds/86400)} дн.'


class ScreenService:
    def __init__(self, repo, settings, payment, provisioning):
        self.repo, self.settings = repo, settings
        self.payment, self.provisioning = payment, provisioning
        self.subscriptions = SubscriptionService(repo, settings.device_limit)
        self.promos = PromoService(repo)

    async def vpn_ready(self):
        from sqlalchemy import select, func
        from app.database.models import VpnServer, RemnawaveServer
        if await self.repo.session.scalar(select(func.count()).select_from(RemnawaveServer)):return True
        return bool(await self.repo.session.scalar(select(func.count()).select_from(VpnServer).where(VpnServer.status=='active',VpnServer.health!='offline')))

    async def render(self, action, user):
        from app.bot.latency import rendering
        from app.services.paid_navigation import contextualize, split
        base, sid = split(action)
        with rendering():
            screen = await self.render_timed(base, user)
        if sid is None and re.fullmatch(r'(?:subscription_extra:\d+:paid|paid_help:\d+(?::.*)?)', base):
            sid = int(base.split(':')[1])
        if screen.appearance_page == 'receipt':
            return screen
        return contextualize(screen, base, sid) if sid is not None else screen

    async def render_timed(self, action, user):
        # Administration includes direct SQL edits/deletes of settings. Keep those
        # controls fully fresh; their small menus do not need the catalogue cache.
        if action.startswith('admin:'):
            return await self.render_cached(action,user)
        async with self.repo.screen_cache():
            return await self.render_cached(action,user)

    async def render_cached(self, action, user):
        origin=re.fullmatch(r'(balance|deposit|ledger(?::(?:payments|balance):\d+)?)~c(\d+)',action)
        if origin:
            base,pid=origin.groups()
            screen=await self.render_cached(base,user)
            mapped=[]
            for row in screen.rows:
                items=[]
                for label,target in row:
                    new=target
                    if base=='balance' and label.startswith('←'):new=f'checkout_view:{pid}'
                    elif target in ('balance','deposit','ledger') or target.startswith('ledger:'):new=target+'~c'+pid
                    if new!=target and target in screen.button_options:screen.button_options[new]=screen.button_options.pop(target)
                    items.append((label,new))
                mapped.append(items)
            screen.rows=mapped
            return screen
        from app.services.list_pages import split_action,paginate
        action,list_offset=split_action(action)
        if action.startswith('custom:'):
            token=action.split(':',1)[1]
            raw=await self.repo.setting('custom_button:'+token)
            try: custom=json.loads(raw)
            except (json.JSONDecodeError,TypeError): custom={}
            if not custom or not custom.get('visible',True):
                return self.missing()
            target=str(custom.get('target',''))
            from app.config.admin_content import CUSTOM_BUTTON_TARGETS
            if target not in CUSTOM_BUTTON_TARGETS:
                return self.missing()
            action=target
        if self.settings.role(user.telegram_id)=='partner':
            if action=='home':
                action='partner:home'
            elif action in ('buy','plans') or action.startswith(('buy_devices:','catalog_groups:0','catalog_table:0')):
                action='partner:plans'
            elif action.startswith(('renew:', 'renew_devices:', 'catalog_groups:', 'catalog_table:',
                                    'up:', 'autorenew:', 'pay:', 'pay_balance:', 'tariff:', 'checkout:',
                                    'admin_free:', 'wpay:', 'balance_buy:')):
                return Screen('Откройте партнёрский раздел для выдачи ключа.',
                              [[('Партнёрский раздел','partner:home')]],structured=True)
        if action.startswith('partner:'):
            from app.services.partner_bot import PartnerService
            return await self.decorate(action,user,await PartnerService(self.repo,self.settings).render(action,user))
        if action.startswith('admin:'):
            from app.services.admin import AdminService
            screen=await AdminService(self.repo, self.settings).render(action, user)
            screen.clear_keyboard=await self.repo.setting(f'gift_keyboard:{user.id}')=='1'
            if screen.clear_keyboard:
                await self.repo.set_setting(f'gift_keyboard:{user.id}','0')
            return paginate(screen,action,list_offset)
        if action.startswith('ag:'):
            from app.services.bot_antig_addon import Addon
            return await Addon(self.repo,self.settings).render(action,user)
        if action.startswith('g:'):
            from app.services.gifts import GiftService
            screen = await GiftService(self.repo, self.settings).render(action, user)
        elif action.startswith('p:'):
            from app.services.proxies import ProxyService
            screen = await ProxyService(self.repo, self.settings).render(action, user)
        elif action.startswith('v:'):
            from app.services.vpn import VpnService
            screen = await VpnService(self.repo, self.settings).render(action, user)
        elif action.startswith('t:'):
            from app.services.tickets import TicketService
            screen = await TicketService(self.repo, self.settings).render(action, user)
        elif action.startswith('x:'):
            from app.services.control import ControlService
            screen = await ControlService(self.repo, self.settings).render(action, user)
        else:
            screen = await self._render(action, user)
        screen=await self.decorate(action,user,screen)
        if screen.appearance_page=='buy' and screen.rich:
            from app.services.bot_catalog import attach_media
            screen=await attach_media(self.repo,screen)
        return paginate(screen,action,list_offset)

    async def decorate(self, action, user, screen):
        if screen.appearance_applied or action.startswith("admin:"):return screen
        screen.appearance_applied=True
        paid_receipt = screen.appearance_page == 'receipt' and any(
            target.endswith(':paid') and target.startswith('subscription_extra:')
            for row in screen.rows for _, target in row
        )
        if action.split(':')[0] in ('sub','keydetail'):
            retained=[]
            seen=set()
            partner=self.settings.role(user.telegram_id)=='partner'
            for row in screen.rows:
                for label,target in row:
                    if partner and target.startswith(('renew:','up:','autorenew:')):
                        continue
                    if target not in seen and (target.startswith(('install:','instructions:','video_instruction:','devices:','copy:','renew:','up:','rename:','autorenew:','ag:','url:','subscription_extra:','key_extra:','g:input:subscription:','v:','p:home')) or (target.startswith('demo_download:') and target.endswith(':config'))):
                        seen.add(target)
                        retained.append([('📲 Установить подписку' if target.startswith('v:connection:') else '🎁 Подарить другу' if target.startswith('g:input:subscription:') else label,target)])
            if retained or (action.startswith('keydetail:') and screen.text.startswith('🔑')):
                if not any(t.startswith(('subscription_extra:','key_extra:')) for row in retained for _,t in row):
                    retained.append([('⚙️ Дополнительно',('subscription_extra:' if action.startswith('sub:') else 'key_extra:')+action.split(':')[1])])
                gift_rows=[row for row in retained if any('Подключить прокси' in label for label,_ in row)]
                retained=[row for row in retained if row not in gift_rows]+gift_rows
                screen.rows=retained+[back('subscriptions')]
        from app.config.admin_content import appearance_page
        page=screen.appearance_page or appearance_page(action)
        media_type=await self.repo.setting('media_type:'+page)
        media_file=await self.repo.setting('media_file:'+page) or await self.repo.setting('image:'+page)
        if media_file:
            media_type=media_type or 'photo'
            if isinstance(screen.photo,tuple):
                screen.banner=(media_type,media_file)
            else:
                screen.photo=None;screen.animation=None;screen.video=None
                if media_type=='photo':screen.photo=media_file
                elif media_type=='animation':screen.animation=media_file
                elif media_type=='video':screen.video=media_file
        intro = await self.repo.setting('intro:' + page)
        if intro:
            screen.text = escape(intro) + '\n\n' + screen.text
        if page == 'tariff':
            tariff=await self.repo.tariff(action.split(':')[1])
            description = await self.repo.tariff_setting(tariff,'description') if tariff else ''
            if description:
                screen.text += '\n\n' + escape(description)
        if page in ('balance', 'referral'):
            screen.rows.insert(0, [('💸 Вывести средства', f'x:withdraw:from_{page}'), ('🧾 Заявки', f'x:withdrawals:0:from_{page}')])
        if page=='balance':screen.rows=[[button] for row in screen.rows for button in row]
        if page == 'help':
            screen.rows.insert(0, [('💳 Автоплатежи', 'payment_management')])
        custom_positions={}
        custom_values={} if paid_receipt else await self.repo.settings_prefix('custom_button:')
        for setting_key,raw in custom_values.items():
            try: custom=json.loads(raw)
            except (json.JSONDecodeError,TypeError): continue
            if not custom.get('visible',True) or custom.get('page')!=page: continue
            label=str(custom.get('label','')).strip()[:64]
            target=str(custom.get('target','')).strip()
            if not label or (target not in __import__('app.config.admin_content',fromlist=['CUSTOM_BUTTON_TARGETS']).CUSTOM_BUTTON_TARGETS and not target.startswith('https://')):
                continue
            callback='url:'+target if target.startswith('https://') else 'custom:'+setting_key.split(':',1)[1]
            try: custom_positions[callback]=int(custom.get('position',1000))
            except (TypeError,ValueError): custom_positions[callback]=1000
            screen.rows.append([(label,callback)])
        labels = await self.repo.settings_prefix('button:')
        icons = await self.repo.settings_prefix('button_icon:')
        styles = await self.repo.settings_prefix('button_style:')
        visibility = await self.repo.settings_prefix('button_visible:')
        positions = await self.repo.settings_prefix('button_position:')
        had_back=any(label.startswith("←") for row in screen.rows for label,_ in row)
        resolved_keys={}
        decorated=[]
        for row in screen.rows:
            new_row=[]
            for label,target in row:
                from app.config.admin_content import stable_button_key
                key=stable_button_key(target,action,label)
                resolved_keys[target]=key
                if target.startswith('copy:') and action in ('home','cabinet'):
                    key='copy_password' if 'пароль' in label.casefold() else 'copy_freeman_id'
                if not paid_receipt and not action.startswith('admin:') and visibility.get('button_visible:'+key,'1')=='0' :
                    continue
                if True:
                    original_label=label
                    if not (paid_receipt and target.startswith('rename:')):
                        label=labels.get('button:'+key) or label
                    if key in ('device_view','proxy_access','catalog_devices','tariff') and 'button:'+key in labels:
                        from app.config.admin_content import without_leading_emoji
                        label=label+' · '+(without_leading_emoji(original_label) if icons.get('button_icon:'+key,'').isdigit() else original_label)
                    if key=='autorenew' and ' · ' in original_label and 'button:'+key in labels:
                        from app.config.admin_content import without_leading_emoji
                        label=label+' · '+(without_leading_emoji(original_label) if icons.get('button_icon:'+key,'').isdigit() else original_label).split(' · ',1)[1]
                    if key=='referral' and labels.get('button:'+key,'').strip() in ('','Приглашай и зарабатывай','👥 Приглашай и зарабатывай','Пригласить друга','👥 Пригласить друга'):label='👥 Хочу заработать'
                    label=label[:64]
                    if key=='balance':
                        base=label.split(' · ',1)[0].strip()
                        if 'баланс' not in base.casefold(): base='💰 Баланс'
                        label=(base.split(':',1)[0]+': '+money(await self.repo.balance(user.id)))[:64]
                    icon=icons.get('button_icon:'+key,'')
                    style=styles.get('button_style:'+key,'')
                    opts=dict(screen.button_options.get(target,{}))
                    if 'button_style:'+key in styles and target!='p:purchase' and not (target.startswith('p:access:') and opts.get('style')=='success') and 'Подключить прокси' not in original_label:
                        opts.pop('style',None)
                    subscription_row = action.split(':')[0] == 'subscriptions' and target.startswith(('sub:', 'keydetail:'))
                    if icon.isdigit() and not subscription_row:
                        opts['icon_custom_emoji_id']=icon
                        # Legacy labels often start with an emoji that must be
                        # removed when Telegram renders a custom icon.  Textual
                        # labels such as "Баланс · 3 ₽" must remain intact.
                        from app.config.admin_content import without_leading_emoji
                        label=without_leading_emoji(label)
                    if style in ('primary','success','danger') and target!='p:purchase' and not (target.startswith('p:access:') and opts.get('style')=='success') and 'Подключить прокси' not in original_label:
                        opts['style']=style
                    if target.startswith('p:access:') and screen.button_options.get(target,{}).get('style')=='success':opts['style']='success'
                    if action.split(':')[0]=='subscriptions' and target.startswith(('sub:','keydetail:')) and screen.button_options.get(target,{}).get('style')=='danger':opts['style']='danger'
                    screen.button_options[target]=opts
                new_row.append((label,target))
            if new_row: decorated.append(new_row)
        default_order={key:index for index,key in enumerate(__import__('app.config.admin_content',fromlist=['EDITABLE_BUTTONS']).EDITABLE_BUTTONS)}
        def row_order(row,index):
            if any(label.startswith('←') for label,_ in row) and not any(('button_position:'+resolved_keys.get(target,button_key(target))) in positions for _,target in row): return 2000000+index
            values=[]
            for _,target in row:
                if target in custom_positions:
                    values.append(custom_positions[target]);continue
                from app.config.admin_content import stable_button_key
                key=resolved_keys.get(target,stable_button_key(target,action,row[0][0]))
                raw=positions.get('button_position:'+key,'')
                if raw.lstrip('-').isdigit(): values.append(int(raw))
            return min(values,default=1000000+index)
        screen.rows = decorated if paid_receipt else [
            row for _, row in sorted(enumerate(decorated), key=lambda item: (row_order(item[1], item[0]), item[0]))
        ]
        if action.split(':')[0] in ('buy','catalog_groups'):
            screen.rows.sort(key=lambda row:0 if any(target=='trial' for _,target in row) else 1)
        if action=='home':
            screen.rows=[[button for button in row if button[1]!='trial' and not button[1].startswith('copy:')] for row in screen.rows]
            screen.rows=[row for row in screen.rows if row]
            screen.rows.sort(key=lambda row:0 if any(button_key(target)=='cabinet' for _,target in row) else 1)
        brand = escape(await self.repo.setting('brand', 'F R E E M A N'))
        screen.text = screen.text.replace('F R E E M A N', brand)
        if action == 'home':
            welcome = await self.repo.setting('welcome', 'Оставь границы позади.')
            if welcome:
                screen.text = f'<b>{brand}</b>\n\n{escape(welcome)}\n\n{screen.text}'
            if self.settings.is_admin(user.telegram_id):
                screen.rows.append([('⚙️ Администратор', 'admin:home')])
        if action in ('plans','buy','g:home'):
            screen.rows=[[button] for row in screen.rows for button in row]
        if action=='home':
            mapped={target:(label,target) for row in decorated for label,target in row}
            screen.rows=[[mapped[t] for t in group if t in mapped] for group in [('cabinet',),('buy',),('subscriptions',),('balance',),('referral',),('p:home',),('help',)]]
            # Trial is offered only in the customer purchase catalogue.
            from app.services.web_login import issue_direct
            if self.settings.encryption_key and 'cabinet' in mapped:
                direct='url:'+self.settings.web_base_url+'/auth/bot?'+urlencode({'token':issue_direct(self.settings,user)})
                screen.rows=[[(label,direct if target=='cabinet' else target) for label,target in row] for row in screen.rows]
                screen.button_options[direct]=screen.button_options.get('cabinet',{})
            if self.settings.is_admin(user.telegram_id):
                from sqlalchemy import select,func
                from app.database.models import Withdrawal
                from app.services.support_counter import attention_count
                tickets=await attention_count(self.repo.session)
                payouts=int(await self.repo.session.scalar(select(func.count()).select_from(Withdrawal).where(Withdrawal.status=='pending')) or 0)
                position=next((i+1 for i,row in enumerate(screen.rows) if any(target=='p:home' for _,target in row)),len(screen.rows))
                screen.rows[position:position]=([[(f'🗂 Обращения · {tickets}','support_staff')]] if self.settings.can_admin_action(user.telegram_id,'admin:t:home') else [])+([[(f'💸 Заявки на выплату · {payouts}','admin:x:withdrawals:0')]] if self.settings.is_owner(user.telegram_id) else [])
                from app.services.admin_hidden import SECTIONS,visible
                hidden_targets={target for key,(_,target,_) in SECTIONS.items() if not await visible(self.repo,key)}
                screen.rows=[[button for button in row if button[1] not in hidden_targets and not ('cabinet' in hidden_targets and button[1].startswith('url:') and '/auth/bot?' in button[1])] for row in screen.rows]
                screen.rows=[row for row in screen.rows if row]
                screen.rows.append([('⚙️ Администратор','admin:home')])
            from app.services.public_proxy import connection_url
            public_proxy = await connection_url(self.repo)
            if public_proxy and not self.settings.is_owner(user.telegram_id):
                for row in screen.rows:
                    for i, (label,target) in enumerate(row):
                        if target == 'p:home':
                            link = 'url:' + public_proxy
                            row[i] = label, link
                            screen.button_options[link] = screen.button_options.get(target, {})
        if not paid_receipt and action!='home' and not had_back and not any(label.startswith('←') for row in screen.rows for label,_ in row):
            screen.rows.append(back())
        gifts=[row for row in screen.rows if any(target==screen.gift_target or 'Подключить прокси' in label for label,target in row)]
        if gifts:
            screen.rows=[row for row in screen.rows if row not in gifts]
            position=next((i for i,row in enumerate(screen.rows) if any(target.startswith(('subscription_extra:','key_extra:')) for _,target in row)),max(0,len(screen.rows)-1))
            screen.rows[position:position]=gifts
            if screen.gift_target:screen.button_options[screen.gift_target]={**screen.button_options.get(screen.gift_target,{}),'style':'primary'}
        if action in ('help','support') and self.settings.can_admin_action(user.telegram_id,'admin:t:home'):
            from sqlalchemy import select,func
            from app.services.support_counter import attention_count
            count=await attention_count(self.repo.session)
            screen.rows=[[button for button in row if button[1] not in ('t:input:new','t:list:0')] for row in screen.rows]
            screen.rows=[row for row in screen.rows if row]
            screen.rows.insert(0,[(f'🗂 Обращения · {count}','support_staff')])
            screen.text='📖 <b>Помощь</b>\n\n🗂 <b>Обращения</b>\nВсе обращения клиентов, переписка и управление статусами — в Mini App. На кнопке указано число диалогов, ожидающих поддержки или находящихся в работе. Диалоги, ожидающие клиента, не учитываются.\n\n💳 <b>Автоплатежи</b>\nУправление картой и автопродлением.\n\n📄 <b>Документы</b>\nУсловия использования, конфиденциальность, оплата и возвраты.'
            screen.structured=True
        if self.settings.encryption_key:
            from app.services.web_login import issue_direct
            for i,row in enumerate(screen.rows):
                for j,(label,target) in enumerate(row):
                    mode='staff' if target=='support_staff' else 'new' if target=='t:input:new' else 'list' if target=='t:list:0' else None
                    if mode:
                        url='webapp:'+self.settings.web_base_url+'/auth/bot/support?'+urlencode({'mode':mode,'token':issue_direct(self.settings,user,purpose='direct_support:'+mode)})
                        screen.rows[i][j]=(label,url)
                        screen.button_options[url]=screen.button_options.get(target,{})
        from app.services.promo_visibility import enabled
        if not await enabled(self.repo):
            screen.rows=[[b for b in row if not b[1].startswith('promo:')] for row in screen.rows]
            screen.rows=[row for row in screen.rows if row]
        screen.clear_keyboard=await self.repo.setting(f'gift_keyboard:{user.id}')=='1'
        if screen.clear_keyboard:
            await self.repo.set_setting(f'gift_keyboard:{user.id}','0')
        return screen

    async def tariff_label(self, tariff):
        return await self.repo.tariff_setting(tariff, 'tariff_name', duration(tariff.months))

    async def subscription_name(self, subscription):
        return await self.repo.setting(f'subscription_name:{subscription.id}', f'F R E E M A N · #{subscription.id}')

    async def subscription_list_label(self, sub, key=None):
        from app.services.subscription_labels import current
        name, _ = await current(self.repo, sub, key)
        return name

    async def _render(self, action, user):
        if action=='trial_offer:claim':
            from app.services.trial_offer import claim
            return await claim(self,user)
        if action.startswith('cg:'):
            from app.services.customer_gifts import CustomerGifts
            return await CustomerGifts(self.repo,self.settings).render(action,user)
        if action.startswith('receipt:') and await self.repo.setting('customer_gift_order:'+action.split(':')[1]):
            from app.services.customer_gifts import CustomerGifts
            return await CustomerGifts(self.repo,self.settings).receipt(int(action.split(':')[1]),user)
        if action.startswith('up:'):
            from app.services.bot_upgrade import BotUpgrade
            return await BotUpgrade(self.repo,self.settings).render(action,user)
        if action.startswith("vr:"):
            from app.services.vless_rotation import render
            return await render(self.repo,self.settings,user,action)
        if action.startswith("vd:"):
            from app.services.vless_devices import render
            return await render(self.repo,self.settings,user,action)
        if action.startswith(('sub:', 'connection:', 'install:', 'devices:', 'device_rotate:', 'device_rotate_apply:')):
            from sqlalchemy import select
            from app.database.models import VlessCredential
            sid=int(action.split(':')[1]);sub=await self.repo.subscription(user.id,sid)
            c=await self.repo.session.scalar(select(VlessCredential).where(VlessCredential.subscription_id==sid,VlessCredential.user_id==user.id)) if sub else None
            if c:
                if action.startswith('devices:'):
                    from app.services.vless_devices import render
                    return await render(self.repo,self.settings,user,f'vd:list:{sid}:0')
                if action.startswith('device_rotate:'):
                    return Screen('Отвязать все устройства? Срок подписки сохранится. Устройства смогут зарегистрироваться снова, если у них осталась ссылка подписки.',[[('Да, отвязать все',f'device_rotate_apply:{sid}')],[('Отмена',f'devices:{sid}')]],button_options={f'device_rotate_apply:{sid}':{'style':'danger'}})
                if action.startswith('device_rotate_apply:'):
                    if c.status!='active' or sub.expires_at.replace(tzinfo=timezone.utc)<=now():raise ValueError('Подписка недоступна.')
                    from app.services.remnawave import RemnawaveService,RemnawaveApiError
                    service=RemnawaveService(self.repo,self.settings);panel=await service.server(c.server_id)
                    try:await service.api(panel).delete_all_devices(c.remote_user_uuid)
                    except RemnawaveApiError as e:raise ValueError('Панель не подтвердила сброс устройств.') from e
                    from app.services.vless_hwid import confirmed_removal
                    await confirmed_removal(self.repo,c)
                    return Screen('Привязки устройств сброшены.',[[('📲 Подключить',f'connection:{sid}')],[('← Назад',f'sub:{sid}')]])
                from app.services.vless_provision import connection
                screen=await connection(self.repo,self.settings,sub,user)
                if screen:return screen
        parts = action.split(':')
        page = parts[0]
        if page == 'paid_help':
            # Old messages contain callback buttons. Refresh their receipt in place.
            return await self._render(f'paid_receipt:{int(parts[1])}', user)
        if page == 'paid_receipt':
            from app.services.vless_provision import connection
            sub = await self.repo.subscription(user.id, int(parts[1]))
            screen = await connection(self.repo, self.settings, sub, user, paid=True, restored=True) if sub else None
            return screen or self.missing()
        if page == 'install_help':
            sid = int(parts[1])
            sub = await self.repo.subscription(user.id, sid)
            if not sub or user.status != 'active':
                return self.missing()
            if len(parts) == 3 and parts[2] == 'close':
                await self.repo.set_setting(f'ai_dialog:{user.id}', '0')
                return await self._render(f'subscription_extra:{sid}:grant', user)
            from app.services.admin_client_grant import rich
            from app.services.freeman_ai.identity import channel_identity
            from app.services.freeman_ai.service import channel_enabled, conversation
            rows = [[('📖 Инструкция по установке', f'install:{sid}')],
                    [('💬 Написать в поддержку', 't:input:new')]]
            if await channel_enabled(self.repo.session, self.settings, 'telegram'):
                identity = await channel_identity(self.repo.session, self.settings, 'telegram', str(user.telegram_id))
                await conversation(self.repo.session, self.settings, identity=identity)
                await self.repo.set_setting(f'ai_dialog:{user.id}', '1')
                title = '💬 <b>Помогу подключить VPN</b>'
                body = ('Я Freeman AI. Напишите, какое у вас устройство и на каком шаге возникла трудность. '
                        'Например: «У меня iPhone, куда вставить ссылку?»')
                rows.append([('← Завершить диалог', f'install_help:{sid}:close')])
            else:
                title = '💬 <b>Помощь с установкой VPN</b>'
                body = 'Откройте инструкцию для своего устройства или напишите в поддержку.'
                rows.append([('← Дополнительно', f'subscription_extra:{sid}:grant')])
            return rich(title+'\n\n'+body, rows)
        if page in ('subscription_extra','key_extra'):
            from app.services.keys import KeyService
            if page=='subscription_extra':
                sub=await self.repo.subscription(user.id,int(parts[1]))
                if not sub:
                    return self.missing()
                kid=await self.repo.setting(f'subscription_key:{sub.id}')
                key=await KeyService(self.repo,self.settings).owned(user.id,int(kid)) if kid else None
            else:
                key=await KeyService(self.repo,self.settings).owned(user.id,int(parts[1]))
                if not key:
                    return self.missing()
                sub=None
                for candidate in await self.repo.subscriptions(user.id):
                    if await self.repo.setting(f'subscription_key:{candidate.id}')==str(key.id):
                        sub=candidate
                        break
            rows=[]
            if sub:
                enabled=await self.repo.setting(f'auto_renew:{sub.id}','0')=='1'
                from app.database.models import VlessCredential
                vless=await self.repo.session.scalar(__import__('sqlalchemy').select(VlessCredential).where(VlessCredential.subscription_id==sub.id,VlessCredential.user_id==user.id))
                if vless:
                    rows.append([('🔐 Перевыпустить ключ',f'vr:confirm:{sub.id}')])
                    rows.append([('🧾 История',f'history:{sub.id}')])
                else:
                    rows.extend([[('🔄 Автопродление · '+('Вкл.' if enabled else 'Выкл.'),f'autorenew:{sub.id}')],[('✏️ Назвать подписку',f'rename:{sub.id}')],[('🧾 История',f'history:{sub.id}')]])
                rows.append([('🗑 Удалить подписку и отозвать ключ',f'sub_control:delete:{sub.id}')])
            if key:
                if not sub and key.status in ('active','frozen','billing_blocked'):
                    rows.append([('🗑 Удалить',f'key_control:delete:{key.id}')])
                if key.status=='gift_pending':
                    rows.append([('🎁 Управление подарками','g:list')])
            text='⚙️ <b>Дополнительно</b>\n\nИстория — покупки и изменения подписки.\nПоддержка — помощь с подключением.\nУдаление отзывает ключ без возврата оставшегося срока.'
            if sub and page == 'subscription_extra' and parts[2:] == ['paid']:
                from app.services.bot_antig_price import enabled as antig_enabled
                from app.services.proxy_control import config, gift_visible, subscription_eligible
                from app.services.public_proxy import connection_url
                active = (sub.expires_at.replace(tzinfo=timezone.utc) > now()
                          and await self.repo.setting(f'subscription_status:{sub.id}', 'active') == 'active'
                          and await self.repo.setting(f'subscription_removed:{sub.id}') != '1')
                extra = [[('📱 Подключённые устройства', f'devices:{sub.id}')],
                         [('🔄 Продлить', f'renew:{sub.id}')]]
                if sub.kind != 'trial':
                    extra.append([('🔁 Автопродление · '+('Вкл.' if enabled else 'Выкл.'), f'autorenew:{sub.id}')])
                if active and sub.kind == 'paid' and sub.tariff_id:
                    extra.append([('✨ Улучшить тариф', f'up:menu:{sub.id}')])
                if active and vless and await antig_enabled(self.repo):
                    extra.append([('🛡 Антиглушилка', f'ag:menu:{sub.id}')])
                public_proxy = await connection_url(self.repo)
                if public_proxy:
                    extra.append([('✈️ Бесплатный Telegram Proxy', 'url:' + public_proxy)])
                elif (active and (await config(self.repo))['auto_included']
                        and await gift_visible(self.repo, sub) and await subscription_eligible(self.repo, sub)):
                    extra.append([('🌐 Подарочный Telegram Proxy', 'p:home')])
                return Screen('⚙️ <b>Управление подпиской</b>', extra + rows + [back(f'sub:{sub.id}')],
                              structured=True)
            if sub and page == 'subscription_extra' and parts[2:] == ['grant']:
                from app.services.admin_client_grant import rich
                extra = [[('💬 Помощь с установкой', f'install_help:{sub.id}')],
                         [('📱 Подключённые устройства', f'devices:{sub.id}')],
                         [('🔄 Продлить', f'renew:{sub.id}')],
                         [('🔁 Автопродление · '+('Вкл.' if enabled else 'Выкл.'), f'autorenew:{sub.id}')],
                         [('✏️ Имя ключа', f'rename:{sub.id}')]]
                from app.services.bot_antig_price import enabled as antig_enabled
                if vless and await antig_enabled(self.repo):
                    extra.append([('🛡 Антиглушилка', f'ag:menu:{sub.id}')])
                return rich('⚙️ <b>Подписка и подключение</b>\n\n'
                            'Нужна помощь с установкой? Подскажем, какое приложение выбрать и куда вставить ссылку.',
                            extra + rows + [back(f'sub:{sub.id}')])
            return Screen(text,rows+[[('💬 Поддержка','support')],back(f'sub:{sub.id}' if sub else f'keydetail:{key.id}:0')])
        if page in ('topup','topup_pay','topup_check'):
            from app.services.topups import TopupService
            screen=await TopupService(self.repo,self.settings).render(action,user)
            if page in ('topup_pay','topup_check'):
                antig_token=await self.repo.setting('bot_antig_topup:'+parts[1])
                if antig_token:screen.rows.insert(0,[('🛡 К подключению антиглушилки',f'ag:show:{antig_token}')])
                token=await self.repo.setting('bot_upgrade_topup:'+parts[1])
                if token:
                    from app.services.bot_upgrade import BotUpgrade
                    await BotUpgrade(self.repo,self.settings).saved(user,token)
                    screen.rows.insert(0,[('✨ Вернуться к улучшению',f'up:show:{token}')])
            return screen
        if page in ('checkout','checkout_view','checkout_check','checkout_balance'):
            from app.services.bot_checkout import BotCheckout
            return await BotCheckout(self.repo,self.settings).render(action,user)
        if page in ('order_pay','order_check'):
            from app.services.topups import SubscriptionCheckoutService
            return await SubscriptionCheckoutService(self.repo,self.settings).render(action,user)
        if page=='topup_input':
            return Screen('Введите сумму пополнения в рублях (1–100 000 ₽), например 500 или 250.50.',[back('balance')])
        if page in ('autorenew','balance_buy','daily'):
            from app.services.billing import BillingService
            return await BillingService(self.repo,self.settings).render(action,user)
        if page in ('key_control','key_apply','sub_control','sub_apply'):
            from app.services.client_keys import ClientKeyService
            return await ClientKeyService(self.repo,self.settings).render(action,user)
        if page=='demo_download':
            sid=int(parts[2])
            sub=await self.repo.subscription(user.id,sid)
            if not sub or await self.repo.setting(f'subscription_status:{sid}','active')!='active' or await self.repo.setting(f'subscription_key:{sid}')!=parts[1]:
                return self.missing()
            from app.services.demo_keys import key_screen
            return await key_screen(self.repo,self.settings,user,int(parts[1]),sid,parts[3] if len(parts)>3 else 'config')
        if page=='campaign_deal':
            from app.services.campaign_buttons import activate
            return await activate(self.repo,action,user,self.settings)
        if page=='campaign_post':
            from app.web.posts import load_json
            value=await load_json(self.repo,'campaign_post:'+parts[1])
            if not value:raise ValueError('Пост недоступен.')
            return Screen('<b>'+escape(value['title'])+'</b>\n\n'+escape(value['text']),[back('home')],structured=True)
        if page == 'bantig':
            from app.services.bot_widget_checkout import button_checkout
            return await button_checkout(self.repo, self.settings, user, parts)
        if page=='wpay':
            from app.services.bot_widget_checkout import render
            return await render(self,action,user)
        if page=='antig_test':
            from app.services.bot_antig_test import render
            return await render(self,action,user)
        if page=='admin_free':
            from app.services.admin_subscription import grant
            sid=await grant(self.repo,self.settings,user,parts[1],int(parts[2]),parts[3],complimentary=len(parts)==5 and parts[4]=='free')
            if await self.vpn_ready():
                from app.services.vpn import VpnService
                return await VpnService(self.repo,self.settings).render(f'v:locations:{sid}',user)
            return await self._render(f'sub:{sid}',user)
        if page in ('tg_auth','tg_approve'):
            from app.services import telegram_auth
            token=parts[1] if len(parts)>1 else ''
            if page=='tg_approve':
                approved=await telegram_auth.approve(self.repo.session,token,user)
                return Screen('Вход подтверждён. Вернитесь в браузер — личный кабинет откроется автоматически.' if approved else 'Запрос истёк или уже подтверждён. Начните вход на сайте снова.',[back('home')])
            row=await telegram_auth.challenge(self.repo.session,token)
            if not row or row.user_id is not None: return Screen('Запрос истёк или уже подтверждён. Начните вход на сайте снова.',[back('home')])
            return Screen(f'<b>Вход в Freeman ONE</b>\n\nАккаунт: {escape(display_freeman_id(user.freeman_id))}\nПодтвердите вход только если вы сами начали его на сайте. Не пересылайте ссылку.\n\nПосле подтверждения вернитесь в тот же браузер.',[[('Подтвердить вход',f'tg_approve:{token}')],back('home')])
        if page == 'home':
            rows=[]
            from app.database.models import User
            from sqlalchemy import select
            from app.services.web_login import issue_direct
            link=f'{self.settings.web_base_url}/auth/bot?{urlencode({"token":issue_direct(self.settings,user)})}' if self.settings.encryption_key else self.settings.web_base_url
            rows = [[('🏠 Личный кабинет','cabinet'),('🛒 Купить подписку','buy')],
                [('🔑 Мои ключи','subscriptions')],
                [('💰 Баланс','balance'),('👥 Хочу заработать','referral')],
                [('✈️ Telegram Proxy','p:home')],[('💬 Помощь','help')]]
            subscriptions=await self.repo.subscriptions(user.id)
            identity=display_freeman_id(user.freeman_id or 'FRM-'+format(user.id,'06d'))
            login=escape(user.account_username or identity)
            from app.services.account_credentials import credentials
            # An automatic support exit must never provision/reset login credentials.
            saved=await self.repo.setting(f'account_credentials209:{user.id}') if getattr(self,'preserve_credentials',False) else True
            password=await credentials(self.repo,self.settings,user) if saved else None
            text=(f'👤 <b>Данные для входа в личный кабинет</b>\n\n'
                  f'Freeman ID: <code>{escape(identity)}</code>\n\n'
                  f'Пароль: {"<code>"+escape(password)+"</code>" if password else "Установлен вами"}')

            return Screen(text, rows)
        if page == 'cabinet':
            from app.services.web_login import issue_direct
            if not self.settings.encryption_key:return Screen('Вход временно недоступен.',[back()])
            link=self.settings.web_base_url+'/auth/bot?'+urlencode({'token':issue_direct(self.settings,user)})
            return Screen('Личный кабинет Freeman ONE\n\nКнопка откроет сайт с выполненным входом. Ссылка действует 10 минут. Не пересылайте её.',[[('Открыть личный кабинет','url:'+link)],[('Настроить / сменить пароль','cabinet_reset')],back('home')])
        if page in ('cabinet_reset','cabinet_reset_confirm'):
            from app.services.web_login import issue_direct
            try:token=issue_direct(self.settings,user,'password_setup')
            except ValueError:return Screen('Настройка пароля временно недоступна. Обратитесь в поддержку.',[back('cabinet')])
            link=f'{self.settings.web_base_url}/account?{urlencode({"setup":token})}'
            return Screen('🔐 <b>Пароль личного кабинета</b>\n\nОткройте защищённую страницу и задайте новый пароль. Ссылка действует 10 минут и используется один раз. Не пересылайте её.',[[('Установить / сменить пароль',f'url:{link}')],back('home')])
        if page=='payment_master':
            if parts[1] not in ('0','1'):raise ValueError('Действие недоступно.')
            await self.repo.set_setting(f'auto_master:{user.id}',parts[1])
            return await self._render('payment_management',user)
        if page=='payment_card':
            from app.services.recurring_cards import methods
            card=next((m for m in await methods(self.repo,user.id,include_expired=True) if hashlib.sha256(m['id'].encode()).hexdigest()[:16]==parts[1]),None)
            if not card:raise ValueError('Карта недоступна.')
            if len(parts)>2 and parts[2]=='remove':
                card['active']=False;await self.repo.set_setting(f'card:{user.id}:{card["id"]}',json.dumps(card))
                for sub in await self.repo.subscriptions(user.id):
                    if await self.repo.setting(f'auto_card:{sub.id}')==card['id']:
                        await self.repo.set_setting(f'auto_renew:{sub.id}','0')
                        await self.repo.set_setting(f'auto_card:{sub.id}','')
                return await self._render('payment_management',user)
            return Screen(f'💳 {escape(card.get("brand","Карта"))} · {escape(card.get("last4",""))}\n\nУдаление отключит автопродление подписок, использующих эту карту.',[[('🗑 Отвязать карту',f'payment_card:{parts[1]}:remove')],back('payment_management')])
        if page == 'card_bind_info':
            from app.services.web_login import issue_direct
            sid=int(parts[1]) if len(parts)>1 else 0
            if sid and not await self.repo.subscription(user.id,sid):return self.missing()
            try:token=issue_direct(self.settings,user,'card_bind')
            except ValueError:return Screen('Привязка карты временно недоступна.',[back('payment_management')])
            link=self.settings.web_base_url+'/account/card-link?'+urlencode({'token':token})
            return Screen('💳 <b>Как привязать карту</b>\n\n1. Нажмите «Перейти в ЮKassa». Откроется защищённая страница платёжного сервиса.\n\n2. Введите данные карты и подтвердите сохранение и платёж 1 ₽. Этот рубль поступит на ваш баланс Freeman ONE.\n\n3. Вернитесь в бот и откройте «Выбрать сохранённую карту» у нужного ключа. Выберите карту и срок продления.\n\nПривязка карты сама по себе не включает автопродление. Реквизиты карты вводятся только в ЮKassa; бот их не получает. Автопродление можно отключить в любое время.',[[('💳 Перейти в ЮKassa · 1 ₽','url:'+link)],[('✅ Карта уже привязана — выбрать',f'autorenew:{sid}:cards' if sid else 'payment_management')],back(f'autorenew:{sid}' if sid else 'payment_management')])
        if page == 'payment_subscriptions':
            from app.services.recurring_cards import renewal_status
            subs=[sub for sub in await self.repo.subscriptions(user.id) if await self.repo.setting(f'subscription_removed:{sub.id}')!='1' and await self.repo.setting(f'subscription_transferred:{sub.id}')!='1']
            offset=max(0,int(parts[1])) if len(parts)>1 else 0
            rows=[]
            for sub in subs[offset:offset+5]:
                state=await renewal_status(self.repo,self.settings,user,sub)
                rows.append([(('🟢 ' if state['effective'] else '⚪ ')+await self.subscription_list_label(sub),f'autorenew:{sub.id}')])
            if offset:rows.append([('⬅️ Предыдущие',f'payment_subscriptions:{max(0,offset-5)}')])
            if len(subs)>offset+5:rows.append([('➡️ Следующие',f'payment_subscriptions:{offset+5}')])
            return Screen('🔑 <b>Автопродление подписок</b>\n\nВыберите подписку, затем карту и период продления. Здесь же можно отключить её автопродление.'+('\n\nПодписок пока нет.' if not subs else ''),rows+[back('payment_management')])
        if page == 'payment_management':
            from app.services.recurring_cards import methods
            cards=await methods(self.repo,user.id,include_expired=True);master=await self.repo.setting(f'auto_master:{user.id}','1')=='1'
            rows=[[('➕ Привязать карту через ЮKassa','card_bind_info')]]
            rows.extend([[(f'💳 {m.get("brand","Карта")} · {m.get("last4","")} · управление',f'payment_card:{hashlib.sha256(m["id"].encode()).hexdigest()[:16]}')] for m in cards])
            rows.extend([[('🔑 Подписки и автопродление','payment_subscriptions')],[('⏸ Отключить все автоплатежи' if master else '▶️ Разрешить автоплатежи',f'payment_master:{0 if master else 1}')],[('🔄 Обновить список карт','payment_management')],back('help')])
            return Screen('💳 <b>Автоплатежи</b>\n\n'+f'Общий режим: {"Включён" if master else "Выключен"}\nСохранённых карт: {len(cards)}\n\n<b>Привязать:</b> нажмите «Привязать карту через ЮKassa». Данные вводятся только на защищённой странице ЮKassa. Платёж 1 ₽ поступит на баланс.\n\n<b>Отвязать:</b> выберите сохранённую карту и нажмите «Отвязать карту». Это отключит автопродление всех подписок с этой картой в Freeman ONE.\n\n<b>Настроить продление:</b> откройте «Подписки и автопродление». Привязка карты сама по себе не включает списания.',rows)
        if page=='plans':
            return await self._render('buy',user)
        if page in ('trial','activate') and self.settings.role(user.telegram_id):
            return Screen('Пробный период доступен только клиентам.',[back('buy')],structured=True)
        if page in ('trial', 'activate'):
            from app.services.trial_offer import claim, eligible, trial_duration, trial_hours, unavailable
            if page == 'activate':
                return await claim(self, user, invited=False)
            if not await eligible(self.repo, self.settings, user):
                return unavailable()
            duration = trial_duration(await trial_hours(self.repo))
            return Screen(f'🎁 <b>Попробуйте VPN бесплатно</b>\n\n{duration} с антиглушилкой на трёх устройствах. '
                          'Пробный период доступен один раз и начнётся после активации.',
                          [[('🎁 Активировать пробный период', 'activate')], back('plans')], structured=True)
        if page in ('buy','renew','buy_devices','renew_devices','catalog_groups','catalog_table'):
            from app.services.bot_catalog import render
            return await render(self,action,user)
        if page in ('tariff', 'pay', 'pay_balance'):
            tariff = await self.repo.tariff(parts[1])
            months = tariff.months if tariff else 0
            ref = f'id{tariff.id}' if tariff else parts[1]
            if not tariff or await self.repo.tariff_setting(tariff, 'tariff_enabled', '1') != '1':
                return self.missing()
            sid = int(parts[2]) if len(parts) == 3 else None
            if sid and not await self.repo.subscription(user.id, sid):
                return self.missing()
            suffix = f':{sid}' if sid else ''
            from app.services.pricing import quote
            price,discount,special = await quote(self.repo,user.id,tariff,sid or 0)
            yookassa_enabled = bool(self.settings.yookassa_shop_id and self.settings.yookassa_secret_key) and await self.repo.setting('payment_provider:yookassa','1') == '1'
            from app.services.stars import star_amount
            stars_price = await star_amount(self.repo, tariff, price)
            stars_enabled = stars_price > 0 and await self.repo.setting('payment_provider:stars', '0') == '1'
            if self.settings.can_free_subscription(user.telegram_id):
                from app.services.bot_antig_price import enabled as antig_enabled
                if page=='tariff' and not sid and self.settings.is_owner(user.telegram_id) and await antig_enabled(self.repo):
                    from app.services.bot_antig_test import render
                    return await render(self,f'antig_test:tariff:{tariff.id}',user)
                from app.services.admin_subscription import preview
                target=await preview(self.repo,user,ref,sid or 0)
                ready=await self.vpn_ready()
                from app.services.bot_admin_checkout import issue_screen
                return await issue_screen(self,user,tariff,target,sid or 0,ready)
            if page == 'pay':
                await self.repo.track_funnel(user.id,'checkout',str(months))
                if yookassa_enabled:
                    from app.services.topups import SubscriptionCheckoutService
                    return await SubscriptionCheckoutService(self.repo,self.settings).prepare(user,ref,sid or 0,payment_method='sbp')
                page='pay_balance'
            if page == 'pay_balance':
                await self.repo.track_funnel(user.id,'checkout',str(months))
                if await self.repo.balance(user.id)<price:
                    return Screen(await self.payment.checkout(user.id,price)+f'\n\nНа балансе недостаточно средств: {money(await self.repo.balance(user.id))}.',[[('💰 Баланс','balance')],back(f'tariff:{ref}{suffix}')])
                from app.services.billing import BillingService
                target=await BillingService(self.repo,self.settings).preview(user,ref,sid or 0)
                confirmation='Будет продлена эта подписка. Подтвердите списание.' if sid else 'Подтвердите оплату подписки.'
                return Screen(f'Оплата с баланса: {money(price)}\nДоступно: {money(await self.repo.balance(user.id))}\n\n{confirmation}',[[('✅ Оплатить с баланса',target)],back(f'tariff:{ref}{suffix}')])
            from app.services.bot_catalog import catalog_term, devices_label, price_text
            title = f'{devices_label(tariff.device_limit)} · {catalog_term(tariff.months)}'
            text = f'<b>Подписка: {title}</b>\n\nСтоимость: '+price_text(tariff.price,price,html=True)
            if special:
                text += '\nВаша индивидуальная цена'
            text += '\n\nПерейдите к оплате и выберите удобный способ.'
            text += (' После подтверждения оплаты текущий ключ продлится автоматически.' if sid
                     else ' После подтверждения подписка появится автоматически.')
            if sid:
                text = text.replace('<b>Подписка:', '<b>Продление подписки:', 1)
            pay_rows=[[('💳 Перейти к оплате',f'checkout:{ref}{suffix}' if yookassa_enabled else f'pay_balance:{ref}{suffix}')]]
            if stars_enabled:pay_rows.append([(f'⭐ Оплатить Telegram Stars · {stars_price} ⭐', f'stars:{ref}{suffix}')])
            pay_rows.append(back(f'renew_devices:{sid}:{tariff.device_limit}' if sid else f'buy_devices:{tariff.device_limit}'))
            return Screen(text,pay_rows,structured=True)
        if page == 'subscriptions':
            from app.services.bot_subscriptions import render
            return await render(self,user,max(0,int(parts[1])) if len(parts)>1 else 0)
        if page == 'keys':
            offset = max(0, int(parts[1]))
            keys = await self.repo.keys(user.id, offset)
            rows = [[(f'{k.name[:40]} · {time_remaining(k.expires_at)}', f'keydetail:{k.id}:{offset}')] for k in keys[:5]]
            if offset:
                rows.append([('← Предыдущие', f'keys:{max(0, offset-5)}')])
            if len(keys) > 5:
                rows.append([('Следующие →', f'keys:{offset+5}')])
            return Screen('🔑 <b>Мои ключи</b>\n\nТестовые записи. Данные подключения появятся позже.' if keys else 'Вам пока не назначены ключи.', rows + [back('subscriptions')])
        if page == 'keydetail':
            from app.services.keys import KeyService
            key = await KeyService(self.repo, self.settings).owned(user.id, int(parts[1]))
            if not key:
                from app.database.models import AccessKey
                removed=await self.repo.session.get(AccessKey,int(parts[1]))
                if removed and removed.user_id==user.id and removed.status=='deleted':
                    return Screen('Ключ удалён и больше не может использоваться.',[[('📦 Мои подписки','subscriptions')]])
                return self.missing()
            if key.mode=='vless':
                from sqlalchemy import select
                from app.database.models import VlessCredential
                credential=await self.repo.session.scalar(select(VlessCredential).where(VlessCredential.access_key_id==key.id,VlessCredential.user_id==user.id))
                if credential:
                    sub=await self.repo.subscription(user.id,credential.subscription_id)
                    if sub:
                        from app.services.vless_provision import connection
                        return await connection(self.repo,self.settings,sub,user)
            offset = max(0, int(parts[2])) if len(parts) > 2 else 0
            expiry = key.expires_at.replace(tzinfo=timezone.utc) if key.expires_at.tzinfo is None else key.expires_at
            status = 'Доступ приостановлен' if key.status == 'frozen' else 'Истёк' if expiry <= now() else 'Активен'
            if key.status=='gift_pending':
                status='Ожидает активации подарка'
            if key.status=='billing_blocked':
                status='Доступ приостановлен: недостаточно средств'
            rows=[]
            if key.status in ('active','frozen','billing_blocked'):
                rows.append([('🗑 Удалить ключ',f'key_control:delete:{key.id}')])
            if key.status=='gift_pending':
                rows.append([('🎁 Управление подарками','g:list')])
            for sub in await self.repo.subscriptions(user.id):
                if await self.repo.setting(f'subscription_key:{sub.id}')==str(key.id):
                    rows.extend([[('📲 Установить подписку',f'v:connection:{sub.id}')],[('🔄 Продлить',f'renew:{sub.id}')],[('⚙️ Дополнительно',f'subscription_extra:{sub.id}')]])
                    rows.append([('🔄 Автопродление',f'autorenew:{sub.id}')])
                    if key.status=='active' and await self.repo.setting(f'demo_file:{key.id}'):
                        if expiry>now() and await self.repo.setting(f'subscription_status:{sub.id}','active')=='active':
                            from app.services.demo_keys import key_screen
                            return await key_screen(self.repo,self.settings,user,key.id,sub.id,'qr')
                    from app.services.gifts import GiftService
                    try:
                        await GiftService(self.repo,self.settings).subscription_key(sub.id,user)
                    except ValueError:
                        pass
                    else:
                        rows.append([('🎁 Подарить другому',f'g:input:subscription:{sub.id}')])
                    rows.append([('📦 Управление подпиской',f'sub:{sub.id}')])
                    break
            mode='Тестовый образец' if key.mode=='mock' else {'amneziawg3':'AmneziaWG 3.1','amneziawg':'AmneziaWG','xray':'Xray'}.get(key.mode,key.mode)
            return Screen(f'🔑 <b>{escape(key.name)}</b>\n\nКлюч: #{key.id}\nСтатус: {status}\nПротокол: {mode}\nОсталось: {time_remaining(expiry)}\nДействует до: {expiry_date(expiry)}\n\nОдна подписка — один телефон. Не передавайте ссылку, QR-код или файл другим людям.', rows+[back(f'subscriptions:{offset}')])
        if page in ('sub', 'connection', 'devices', 'device_add', 'device_rotate', 'device_rotate_apply', 'history', 'rename', 'install', 'video_instruction'):
            sid = int(parts[1])
            subscription = await self.repo.subscription(user.id, sid)
            if not subscription:
                return self.missing()
            target = f'sub:{sid}'
            if page == 'rename':
                return Screen('✏️ <b>Как назовём ключ?</b>\n\nПришлите название одним сообщением. Например, «Мой iPhone» или «Для дома».\nДо 60 символов.', [back(target)], structured=True)
            if page=='install':
                return Screen('📲 <b>Установка подписки</b>\n\n1. Установите AmneziaVPN или AmneziaWG.\n2. Вернитесь к карточке подписки.\n3. Отсканируйте QR-код, скопируйте ссылку либо скачайте конфигурацию.\n4. Импортируйте ключ в приложение и включите подключение.\n\nОдин ключ предназначен для одного телефона.',[[('📖 Подробные инструкции',f'instructions:{sid}')],[('🎬 Видеоинструкция',f'video_instruction:{sid}')],back(target)])
            if page=='video_instruction':
                if not await self.repo.setting('media_file:video_instruction'):
                    return Screen('🎬 Видеоинструкция пока не загружена. Используйте подробную текстовую инструкцию.',[[('📖 Подробные инструкции',f'instructions:{sid}')],back(target)])
                return Screen('🎬 <b>Видеоинструкция</b>\n\nСледуйте шагам в видео. Если приложение показывает ошибку, не публикуйте ключ — обратитесь в поддержку.',[back(target)])
            frozen = await self.repo.setting(f'subscription_status:{sid}', 'active') == 'frozen'
            if page == 'sub':
                kid=await self.repo.setting(f'subscription_key:{sid}')
                if not frozen and subscription.expires_at.replace(tzinfo=timezone.utc)>now() and kid and await self.repo.setting(f'demo_file:{kid}'):
                    from app.services.demo_keys import key_screen
                    return await key_screen(self.repo,self.settings,user,int(kid),sid,'qr')
                expiry = subscription.expires_at
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                seconds = (expiry - now()).total_seconds()
                active = seconds > 0
                hours = round((subscription.expires_at - subscription.created_at).total_seconds() / 3600)
                label = f'Пробный период · {hours} ч.'
                if subscription.kind!='trial':
                    label='Подписка'
                if subscription.tariff_id:
                    tariff = await self.repo.tariff_by_id(subscription.tariff_id)
                    label = escape(await self.tariff_label(tariff)) if tariff else 'Подписка'
                remaining = f'{ceil(seconds / 3600)} ч.' if seconds <= 86400 else f'{ceil(seconds / 86400)} дн.'
                text = f'{"🟢" if active and not frozen else "⚪"} <b>F R E E M A N</b>\n\nТариф: {label}\nСтатус: {"Доступ приостановлен" if frozen else "Активна" if active else "Истекла"}\nОсталось: {remaining if active else "0 дней"}\nДействует до: {expiry_date(expiry)}'
                text += f'\nНазвание: {escape(await self.subscription_name(subscription))}'
                if frozen and kid and await self.repo.setting(f'client_frozen:{kid}')=='1':
                    until=await self.repo.setting(f'client_freeze_until:{kid}')
                    if until:
                        from datetime import datetime
                        text+=f'\nДоступ восстановится: {datetime.fromisoformat(until):%d.%m.%Y %H:%M} UTC'
                text += '\n\nОдна подписка — один телефон. Не устанавливайте один ключ на несколько устройств и не передавайте его другим людям.'
                rows=[[('📲 Установить подписку',f'install:{sid}')],[('📖 Подробные инструкции',f'instructions:{sid}')],[('🎬 Видеоинструкция',f'video_instruction:{sid}')],[('🔄 Продлить', f'renew:{sid}')],[('⚙️ Дополнительно',f'subscription_extra:{sid}')]]
                key_id=await self.repo.setting(f'subscription_key:{sid}')
                if key_id:
                    from app.services.keys import KeyService
                    owned_key=await KeyService(self.repo,self.settings).owned(user.id,int(key_id))
                    if owned_key:
                        rows.append([('⚙️ Управление ключом',f'keydetail:{key_id}:0')])
                if active and not frozen and key_id and await self.repo.setting(f'demo_file:{key_id}'):
                    rows.insert(0,[('📄 Скачать тестовый ключ',f'demo_download:{key_id}:{sid}')])
                    rows.insert(1,[('📲 Как установить',f'instructions:{sid}')])
                from app.services.gifts import GiftService
                try:
                    await GiftService(self.repo,self.settings).subscription_key(sid,user)
                except ValueError:
                    pass
                else:
                    rows.append([('🎁 Подарить подписку',f'g:input:subscription:{sid}')])
                return Screen(text, rows+[back('subscriptions')])
            if page == 'connection':
                if frozen:
                    return Screen('Подписка заморожена. Обратитесь в поддержку.', [[('💬 Поддержка', 'support')], back(target)])
                from app.services.vpn import VpnService
                return await VpnService(self.repo,self.settings).render(f'v:connection:{sid}',user)
            if page == 'devices':
                from app.services.vpn import VpnService,PROTOCOLS,traffic
                vpn=VpnService(self.repo,self.settings);credential=await vpn.credential(sid,user.id)
                kid=await self.repo.setting(f'subscription_key:{sid}')
                if credential:
                    server=await vpn.server(credential.server_id)
                    last=credential.last_handshake.strftime('%d.%m.%Y %H:%M UTC') if credential.last_handshake else 'подключений ещё не зафиксировано'
                    online=credential.last_handshake and (now()-credential.last_handshake.replace(tzinfo=timezone.utc)).total_seconds()<180
                    text=(f'📱 <b>Моё устройство</b>\n\nУстройство: Основное\nСтатус: {"🟢 активно сейчас" if online else "⚪ не в сети"}\n'
                          f'Сервер: {escape(server.country)} · {escape(server.name)}\nПротокол: {PROTOCOLS.get(credential.protocol,credential.protocol)}\n'
                          f'Последнее подключение: {last}\nТрафик: ↓ {traffic(credential.received)} · ↑ {traffic(credential.sent)}\nЛимит: 1 устройство')
                elif kid:
                    text='📱 <b>Моё устройство</b>\n\nУстройство: Тестовое\nСтатус: конфигурация выпущена, реальные подключения не измеряются\nЛимит: 1 устройство'
                else:
                    return Screen('📱 Устройство ещё не подключено.',[[('📲 Установить подписку',f'install:{sid}')],back(target)])
                text+='\n\nAmnezia/WireGuard не передаёт модель телефона. Бот видит логический ключ, последнюю активность и общий трафик. Перевыпуск отзовёт старую конфигурацию на прежнем телефоне.'
                return Screen(text,[[('🔄 Сменить устройство / перевыпустить ключ',f'device_rotate:{sid}')],back(target)])
            if page == 'device_add':
                return Screen(await self.provisioning.add_device(sid), [back(f'devices:{sid}')])
            if page=='device_rotate':
                return Screen('Перевыпустить ключ для нового устройства? Старая ссылка, QR-код и конфигурация перестанут работать. Оставшийся срок подписки сохранится.',[[('✅ Перевыпустить ключ',f'device_rotate_apply:{sid}')],back(f'devices:{sid}')])
            if page=='device_rotate_apply':
                kid=await self.repo.setting(f'subscription_key:{sid}')
                from app.services.vpn import VpnService
                vpn=VpnService(self.repo,self.settings);credential=await vpn.credential(sid,user.id)
                if credential:
                    await vpn.rotate(subscription,user)
                    return await vpn.render(f'v:connection:{sid}',user)
                if not kid: return Screen('Ключ ещё не выпущен.',[[('📲 Установить подписку',f'install:{sid}')],back(target)])
                from app.services.demo_keys import create_demo_file,key_screen
                from app.database.models import AccessKey
                key=await self.repo.session.get(AccessKey,int(kid))
                if not key or key.user_id!=user.id: return self.missing()
                await create_demo_file(self.repo,key)
                return await key_screen(self.repo,self.settings,user,key.id,sid,'qr')
            return Screen('🧾 <b>История подписки</b>\n\n' + ('Активирован пробный период.' if subscription.kind == 'trial' else 'Пока операций нет.'), [back(target)])
        if page == 'balance':
            amount=money(await self.repo.balance(user.id))
            minimum=money(int(await self.repo.setting('withdraw_min','1000'))*100)
            return Screen(f'💰 <b>Ваш баланс</b>\n\n<b>{amount}</b>\n\nСредства можно потратить на покупку и продление подписок или оформить заявку на вывод.\n\n<b>Вывод средств — от {minimum}.</b>\n\n<blockquote>👥 <b>Зарабатывайте с Freeman ONE</b>\n\nПриглашайте друзей по своей реферальной ссылке и получайте бонусы за их покупки. Чем больше приглашённых, тем выше процент. Условия и ваша ссылка — в реферальной программе.</blockquote>', [[('➕ Пополнить баланс','deposit')],[('🛒 Купить подписку','buy')],[('👥 Заработать с друзьями','referral')],[('💳 Автоплатежи','payment_management')],[('📜 История операций','ledger')],back()],structured=True)
        if page == 'deposit':
            if self.settings.yookassa_shop_id and self.settings.yookassa_secret_key and await self.repo.setting('payment_provider:yookassa','1')=='1':
                return Screen('💳 <b>Пополнить баланс через ЮKassa</b>\n\nВыберите сумму. После подтверждения платежа деньги поступят на баланс, с которого оплачиваются подписки.',[[(money(amount),f'topup:{amount}')] for amount in (10000,30000,50000,100000)]+[[('✍️ Другая сумма','topup_input')],back('balance')],structured=True)
            return Screen('💳 <b>Пополнить баланс</b>\n\nПосле подключения платёжной системы вы сможете самостоятельно выбрать сумму и оплатить её здесь. Деньги поступят на баланс после подтверждения оплаты. Покупки и продления будут списываться с баланса.\n\nСейчас платёжная система ещё не подключена.',[back('balance')])
        if page == 'ledger':
            from sqlalchemy import select
            from app.database.models import Payment,BalanceTransaction
            mode=parts[1] if len(parts)>1 else 'payments'
            if mode not in ('payments','balance'):raise ValueError('История недоступна.')
            offset=max(0,int(parts[2])) if len(parts)>2 else 0
            model=Payment if mode=='payments' else BalanceTransaction
            items=list(await self.repo.session.scalars(select(model).where(model.user_id==user.id).order_by(model.id.desc()).offset(offset).limit(6)))
            names={'manual_paid':'ручная продажа оплачена','manual_unpaid':'ручная продажа не оплачена','manual_preparing':'выдача готовится','manual_received_pending':'оплата получена, выдача проверяется','topup_created':'создано пополнение','topup_pending':'ожидает оплаты','topup_credited':'зачислено','topup_canceled':'отменено','order_created':'создан заказ','order_pending':'ожидает оплаты','order_processing':'обрабатывается','order_paid':'подписка оплачена','order_canceled':'отменено','balance_paid':'оплачено с баланса','stars_created':'счёт Stars создан','stars_pending':'ожидает Stars','stars_processing':'обрабатывается','stars_paid':'оплачено Telegram Stars'}
            text=('📜 <b>Платежи</b>' if mode=='payments' else '📜 <b>Движение баланса</b>')+'\n\n'+'\n\n'.join(f'#{item.id} · {item.created_at:%d.%m.%Y %H:%M} · {money(item.amount)} · '+(names.get(item.status,item.status) if mode=='payments' else escape(item.description)) for item in items[:5])
            if not items:text+='Записей пока нет.'
            rows=[]
            if offset:rows.append([('← Предыдущие',f'ledger:{mode}:{max(0,offset-5)}')])
            if len(items)>5:rows.append([('Следующие →',f'ledger:{mode}:{offset+5}')])
            return Screen(text,rows+[[('Движение баланса' if mode=='payments' else 'Платежи','ledger:balance:0' if mode=='payments' else 'ledger:payments:0')],back('balance')],structured=True)
        if page == 'referral_details':
            from app.services.bot_referral_guide import guide_link
            return Screen('📖 <b>Как это работает</b>\n\nСтупени, примеры заработка и правила начислений — в краткой справке.',
                [[('📖 Открыть справку',await guide_link(self.repo,self.settings))],back('referral')],structured=True)
        if page == 'referral':
            if await self.repo.setting('referral_enabled', '1') != '1':
                return Screen('Реферальная программа сейчас недоступна.', [back()])
            link = f'{self.settings.web_base_url.rstrip('/')}/r/{display_freeman_id(user.freeman_id)}'
            share = 'https://t.me/share/url?' + urlencode({'url': link, 'text': 'F R E E M A N — оставь границы позади.'})
            from sqlalchemy import select, func
            from app.database.models import ReferralReward
            from app.services.referral_accounts import account_level
            from app.services.bot_referral_guide import guide_link
            rates=await account_level(self.repo,user.id)
            earned=await self.repo.session.scalar(select(func.coalesce(func.sum(ReferralReward.amount),0)).where(
                ReferralReward.inviter_id==user.id,ReferralReward.status=='credited'))
            terms='Персональные условия' if rates.get('personal') else f'Ступень {rates["tier"]}'
            text=(f'👥 <b>Хочу заработать</b>\n\n'
                f'{terms} · <b>{rates["first_percent"]}%</b> за первую покупку, <b>{rates["repeat_percent"]}%</b> за повторные.'
                f'\n\n<b>Ваша ссылка</b>\n<code>{escape(link)}</code>'
                f'\n\nПриглашено: <b>{rates["invited"]}</b> · Начислено: <b>{money(earned or 0)}</b>')
            return Screen(text,[[('📤 Поделиться','url:'+share)],
                [('📖 Как это работает и сколько можно заработать',await guide_link(self.repo,self.settings))],
                [('📜 История приглашений','referral_history')],back()],structured=True)
        if page == 'referral_history':
            from sqlalchemy import select
            from app.database.models import Referral
            offset=max(0,int(parts[1])) if len(parts)>1 else 0
            referrals=list(await self.repo.session.scalars(select(Referral).where(Referral.inviter_id==user.id).order_by(Referral.id.desc()).offset(offset).limit(6)))
            rows=[]
            if offset:rows.append([('← Предыдущие',f'referral_history:{max(0,offset-5)}')])
            if len(referrals)>5:rows.append([('Следующие →',f'referral_history:{offset+5}')])
            return Screen('\n'.join(f'{r.created_at:%d.%m.%Y} · приглашение #{r.id}' for r in referrals[:5]) or 'Пока приглашений нет.', rows+[back('referral')])
        if page == 'help':
            return Screen('📖 <b>Помощь</b>\n\n💳 <b>Автоплатежи</b>\nПривязка и удаление карты, управление автопродлением подписок.\n\n💬 <b>Написать в поддержку</b>\nЕсли возникла проблема или вопрос, отправьте сообщение — мы поможем.\n\n🗂 <b>Мои обращения</b>\nВаши сообщения в техподдержку и ответы. Откройте обращение, чтобы посмотреть переписку в Mini App внутри Telegram.\n\n📄 <b>Документы</b>\nПользовательское соглашение, политика конфиденциальности, правила оплаты и возврата.', [[('💬 Написать в поддержку','t:input:new')],[('🗂 Мои обращения','t:list:0')],[('📄 Документы','documents')],back()],structured=True)
        if page == 'instructions':
            sid = parts[1] if len(parts) == 2 else ''
            if sid and not await self.repo.subscription(user.id, int(sid)):
                return self.missing()
            suffix = f':{sid}' if sid else ''
            rows=[]
            for key,(title,_) in INSTRUCTIONS.items():
                rows.append([((await self.repo.setting('instruction_title:'+key)) or title,f'instruction:{key}{suffix}')])
            return Screen('📱 <b>Инструкции по подключению</b>\n\nВыберите своё устройство:',rows+[back(f'connection:{sid}' if sid else 'help')])
        if page == 'instruction':
            title, text = INSTRUCTIONS[parts[1]]
            sid = parts[2] if len(parts) == 3 else ''
            if sid and not await self.repo.subscription(user.id, int(sid)):
                return self.missing()
            title=(await self.repo.setting('instruction_title:'+parts[1])) or title
            text=(await self.repo.setting('instruction:'+parts[1])) or text
            return Screen(f'<b>{escape(title)}</b>\n\n{escape(text)}', [back(f'instructions:{sid}' if sid else 'instructions')])
        if page == 'faq':
            rows=[]
            for index,(question,_) in enumerate(FAQ):
                rows.append([((await self.repo.setting(f'faq_question:{index}')) or question,f'answer:{index}')])
            return Screen('❓ <b>Частые вопросы</b>\n\nВыберите вопрос:',rows+[back('help')])
        if page == 'answer':
            index = int(parts[1])
            if not 0 <= index < len(FAQ):
                return self.missing()
            question, answer = FAQ[index]
            question=(await self.repo.setting(f'faq_question:{index}')) or question
            answer=(await self.repo.setting(f'faq:{index}')) or answer
            return Screen(f'<b>{escape(question)}</b>\n\n{escape(answer)}', [back('faq')])
        if page == 'support':
            return Screen('🛟 <b>Техподдержка</b>\n\nСоздайте обращение и опишите вопрос. Ответ придёт в этот бот. Переписка и статус сохраняются в ваших обращениях.', [[('✍️ Написать в поддержку', 't:input:new')], [('🗂 Мои обращения', 't:list:0')]] + [back('help')])
        if page == 'documents':
            rows=[]
            for index,title in enumerate(DOCUMENTS):
                rows.append([((await self.repo.setting(f'document_title:{index}')) or title,f'document:{index}')])
            from app.services.document_rich import document_layout
            text='Условия использования сервиса и обработки данных.\n\nВыберите документ ниже, чтобы прочитать полный текст.'
            return Screen('📄 <b>Документы</b>\n\n'+text,rows+[back('help')],structured=True,rich=document_layout('📄 Документы',text))
        if page == 'document':
            index = int(parts[1])
            if not 0 <= index < len(DOCUMENTS):
                return self.missing()
            title=(await self.repo.setting(f'document_title:{index}')) or DOCUMENTS[index]
            body=(await self.repo.setting(f'document:{index}')) or DOCUMENT_TEXTS[index]
            from app.services.document_rich import document_layout
            brand=await self.repo.setting('brand','F R E E M A N')
            formatted='\n\n'.join(line.strip() for line in body.splitlines() if line.strip())
            return Screen(f'<b>{escape(title)}</b>\n\n{escape(formatted)}', [back('documents')],structured=True,rich=document_layout(title,body.replace('F R E E M A N',brand)))
        if page == 'promo':
            return Screen('Введите промокод:', [back(action.partition(':')[2])])
        return self.missing()

    def missing(self):
        return Screen('Этот раздел недоступен. Вернитесь в меню.', [back()])
