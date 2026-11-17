"""Recipient-bound grants and explicitly confirmed custom manual sales."""
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from html import escape
from sqlalchemy import select
from aiogram.types import InputRichMessage
from app.database.models import AppSetting, ChatNotice, Payment, Subscription, AccessKey, User, VlessCredential, now
from app.services.accounts import display_freeman_id
from app.services.screens import Screen, duration
from app.bot.rich_copy import text_blocks


def rich(text, rows):
    return Screen(text, rows, rich=InputRichMessage(blocks=text_blocks(text),skip_entity_detection=True),structured=True)


async def delivery_screen(repo, job):
    from app.services.bot_catalog import devices_label
    from app.services.subscription_labels import current, expiry_date
    from app.services.vless_creation import utc

    credential = await repo.session.get(VlessCredential, int(job.dedup.split(':')[1]))
    if not credential or credential.status != 'active' or utc(credential.expires_at) <= now():
        return None
    user = await repo.session.get(User, credential.user_id)
    if not user or user.status != 'active' or user.telegram_id != job.target_id:
        return None
    sub = await repo.subscription(user.id, credential.subscription_id)
    key = await repo.session.get(AccessKey, credential.access_key_id)
    if not sub or not key or key.user_id != user.id or key.status != 'active':
        return None
    if (utc(sub.expires_at) <= now()
            or await repo.setting(f'subscription_status:{sub.id}', 'active') != 'active'
            or await repo.setting(f'subscription_removed:{sub.id}') == '1'
            or await repo.setting(f'subscription_transferred:{sub.id}') == '1'):
        return None
    name, term = await current(repo, sub, key)
    heading = '🎁 Вам подарок!' if job.text == 'gift' else '✨ Вам выдали VPN-подписку'
    connect = 'url:' + credential.subscription_url
    title = 'Ваша VPN-подписка от Freeman ONE уже активна.' if job.text == 'gift' else 'Подписка активна и готова к подключению.'
    plan = f'<b>{escape(name)}</b>' + (f'\n🗓 {escape(term)}' if term and term.casefold() not in name.casefold() else '')
    screen = rich(f'<b>{heading}</b>\n\n{title}\n\n'
                  f'<blockquote>{plan}\n📱 До {devices_label(sub.device_limit)}\n'
                  f'📅 Действует до {expiry_date(sub.expires_at)}</blockquote>\n\n'
                  '<b>Как начать</b>\nНажмите «Подключить» и следуйте подсказкам. '
                  'Если приложение просит ссылку, скопируйте ключ.\n\n'
                  '💬 Если понадобится помощь с установкой, откройте «Дополнительно».',
                  [[('📲 Подключить', connect)], [('📋 Скопировать ключ', 'copy:'+credential.subscription_url)],
                   [('⚙️ Дополнительно', f'subscription_extra:{sub.id}:grant')]])
    screen.button_options[connect] = {'style': 'primary'}
    return screen


def parse_custom(value):
    match=re.fullmatch(r'\s*(\d+)\s*(д|дн\.?|день|дня|дней|дни|ч|час|часа|часов|h|d)?\s*\|\s*(\d+)\s*(?:\|\s*(.{1,80}))?\s*',value,re.I)
    if not match:
        raise ValueError('Введите срок и устройства: 20 дней | 3 или 12 часов | 2. Можно добавить | название ключа.')
    amount,unit,devices,name=match.groups();amount=int(amount);devices=int(devices)
    hours=amount if unit and unit.lower() in ('ч','час','часа','часов','h') else amount*24
    if not 1<=hours<=3650*24 or not 1<=devices<=100:
        raise ValueError('Срок: от 1 часа до 3650 дней. Устройства: от 1 до 100.')
    label=f'{amount} '+('ч.' if unit and unit.lower() in ('ч','час','часа','часов','h') else 'дн.')
    return hours,devices,(name or '').strip(),label


class ClientGrant:
    def __init__(self,repo,settings):
        self.repo,self.settings,self.session=repo,settings,repo.session

    async def customer(self,uid,actor):
        from app.services.admin import AdminService
        if not self.settings.is_owner(actor.telegram_id):
            raise PermissionError('Выдача из карточки доступна только администратору.')
        user=await AdminService(self.repo,self.settings).customer(uid)
        if user.status!='active':raise ValueError('Клиент недоступен для выдачи ключа.')
        return user

    def identity(self,user):
        return f'{escape(user.display_name)} · {escape(display_freeman_id(user.freeman_id) or f"FO-{user.id}")}'

    async def prompt(self,uid,actor):
        user=await self.customer(uid,actor)
        return rich('🎁 <b>Другой срок</b>\n\nКлиент: '+self.identity(user)+'\n\nВведите срок и число устройств:\n<code>20 дней | 3</code>\nили <code>12 часов | 2</code>\n\nНеобязательно добавьте название: <code>20 дней | 3 | Рабочий</code>\n\nПолучатель уже выбран. Выдача бесплатная.',[[('← Назад',f'admin:issue:{uid}')]])

    async def custom(self,uid,value,actor):
        hours,devices,name,label=parse_custom(value)
        return await self.preview(uid,actor,hours=hours,devices=devices,name=name,label=label)

    async def preview(self,uid,actor,*,tariff_id=None,hours=None,devices=None,name='',label='',custom_months=None,return_to=None,account_sale=False,issuance_kind=None,explicit_price=None,card_tariff=False,antig=False,widget_access=None):
        user=await self.customer(uid,actor)
        if issuance_kind not in (None,'replacement') or issuance_kind=='replacement' and account_sale:
            raise ValueError('Замена не может учитываться как продажа.')
        traffic=0;months=None
        if tariff_id:
            tariff=await self.repo.tariff_by_id(tariff_id)
            if not tariff or await self.repo.tariff_setting(tariff,'tariff_enabled','1')!='1' or await self.repo.tariff_setting(tariff,'tariff_deleted','0')=='1':
                raise ValueError('Тариф больше недоступен. Выберите другой.')
            from app.services.billing import purchase_limits,extend
            devices,traffic=await purchase_limits(self.repo,tariff)
            months=tariff.months;label=duration(months)
            expiry=extend(now(),months)
        else:
            if type(devices) is not int or not 1<=devices<=100:
                raise ValueError('Устройства: от 1 до 100.')
            if custom_months is not None:
                if type(custom_months) is not int or not 1<=custom_months<=120:
                    raise ValueError('Срок: от 1 до 120 месяцев.')
                from app.services.billing import extend
                expiry=extend(now(),custom_months)
                label=duration(custom_months)
            else:
                if type(hours) is not int or not 1<=hours<=3650*24:
                    raise ValueError('Срок: от 1 часа до 3650 дней.')
                expiry=now()+timedelta(hours=hours)
        if not isinstance(name,str) or len(name)>80:
            raise ValueError('Название: до 80 символов.')
        name=name or f'{devices} устр. · {label}'
        token=secrets.token_hex(12)
        data=dict(actor=actor.telegram_id,user=uid,tariff=tariff_id,months=months,devices=devices,traffic=traffic,name=name,label=label,end=expiry.isoformat(),expires=now().timestamp()+600)
        if card_tariff:
            from app.services.catalog import public_price
            amount = await public_price(self.repo, 'vpn', tariff.id, tariff.price)
            if antig:
                from app.services.bot_antig_price import require_enabled, tariff_total
                from app.services.bot_antig_test import resolve
                await require_enabled(self.repo)
                widget_access = await resolve(self.repo, self.settings, True)
                amount += await tariff_total(self.repo, tariff)
            data['tariff_sale'] = amount
        if widget_access:
            from app.services.bot_antig_test import validate_profile
            data['widget_access'] = validate_profile(widget_access)
        if issuance_kind:
            data['issuance_kind']=issuance_kind
        if account_sale:
            if tariff_id:raise ValueError('Учёт нестандартной продажи не применяется к шаблонному тарифу.')
            from app.services.bot_custom_sales import quote
            data['custom_sale']=quote(devices,hours=hours,months=custom_months,label=label,explicit_price=explicit_price)
        if return_to is not None:
            if not re.fullmatch(r'admin:grantconfig:(?:view:[0-9a-f]{16}|replacement:[0-9]{1,19})',return_to):
                raise ValueError('Возврат к настройке недоступен.')
            data['return_to']=return_to
        await self.repo.set_setting('client_grant:'+token,json.dumps(data))
        return self.confirmation(user,data,token)

    def confirmation(self,user,data,token):
        custom_sale=data.get('custom_sale')
        amount=custom_sale['total'] if custom_sale else data.get('tariff_sale')
        from app.services.screens import money
        replacement=data.get('issuance_kind')=='replacement'
        note=(f'\n\n<b>Ручная продажа: {money(amount)}</b>\nПодтвердите учёт, если клиент уже оплатил вне бота.'
              if amount is not None else '\n\nВыдача бесплатная.')
        if replacement:
            note='\n\n♻️ <b>Замена — без учёта в продажах.</b> Без оплаты, выручки и начисления на баланс. Старый ключ автоматически не отключается.'
        rows=[[('✅ Выдать и учесть '+money(amount) if amount is not None else '♻️ Выдать без продажи' if replacement else '🎁 Выдать бесплатно',f'admin:clientgrant:apply:{token}')]]
        if amount is not None:
            rows.append([('🎁 Выдать бесплатно',f'admin:clientgrant:free:{token}')])
        if data.get('widget_access'):
            note+='\n\n🛡 Антиглушилка включена на весь срок.'
        back = f'admin:clientgrant:devices:{user.id}:{data["devices"]}' if 'tariff_sale' in data else f'admin:issue:{user.id}'
        rows.append([('← Изменить',data.get('return_to',back))])
        return rich(('♻️ <b>Подписка для замены</b>' if replacement else '🎁 <b>Вы выбрали подписку</b>')+'\n\nКлиент: '+self.identity(user)+f'\n\n<b>Срок:</b> {escape(data["label"])}\n<b>Устройства:</b> {data["devices"]}\n<b>Название:</b> {escape(data["name"])}'+note,rows)

    async def apply(self,token,actor,*,complimentary=False):
        # Navigation can render a provider error and commit the surrounding
        # request. Keep partial issuance and accounting inside a savepoint.
        async with self.session.begin_nested():
            return await self._apply(token, actor, complimentary=complimentary)

    async def _apply(self,token,actor,*,complimentary):
        if not re.fullmatch(r'[0-9a-f]{24}',token):raise ValueError('Подтверждение недоступно.')
        row=await self.session.scalar(select(AppSetting).where(AppSetting.key=='client_grant:'+token).with_for_update().execution_options(populate_existing=True))
        data=json.loads(row.value) if row else {}
        if data.get('actor')!=actor.telegram_id:raise PermissionError('Подтверждение другого администратора.')
        if data.get('widget_client'):
            binding=data['widget_client']
            root=json.loads(await self.repo.setting(binding['root'],'{}'))
            if (root.get('actor')!=actor.id or root.get('client_id')!=data['user']
                    or root.get('admin_issue')!=dict(token=token,selection=binding['selection'])
                    or complimentary!=(binding['selection'][2]=='free')):
                raise PermissionError('Получатель или способ учёта не подтверждён.')
        user=await self.customer(data['user'],actor)
        if data.get('credential'):
            credential=await self.session.get(VlessCredential,data['credential'])
            if not credential or credential.user_id!=user.id:raise ValueError('Выданный ключ недоступен.')
            return await self.issued_screen(user,credential,actor,data)
        if data.get('expires',0)<now().timestamp():
            from app.services.vless_creation import unresolved
            if not await unresolved(self.repo, user, ['bot-client-grant:'+token]):
                raise ValueError('Подтверждение устарело. Откройте выдачу заново.')
        if complimentary:
            data.pop('custom_sale',None)
            data.pop('tariff_sale',None)
            if user.id == actor.id:
                data['issuance_kind']='replacement'
        if data.get('issuance_kind')=='replacement' and data.get('custom_sale'):
            raise ValueError('Замена не может учитываться как продажа.')
        expiry=datetime.fromisoformat(data['end'])
        if expiry<=now():raise ValueError('Срок истёк. Создайте новую выдачу.')
        if data.get('tariff'):
            tariff=await self.repo.tariff_by_id(data['tariff'])
            from app.services.billing import purchase_limits
            if not tariff or await self.repo.tariff_setting(tariff,'tariff_enabled','1')!='1' or await self.repo.tariff_setting(tariff,'tariff_deleted','0')=='1':raise ValueError('Тариф отключён. Выберите другой.')
            devices,traffic=await purchase_limits(self.repo,tariff)
            if (tariff.months,devices,traffic)!=(data['months'],data['devices'],data['traffic']):raise ValueError('Тариф изменился. Подтвердите новые условия.')
        sub=await self.repo.add(Subscription(user_id=user.id,tariff_id=data.get('tariff'),kind='admin',expires_at=expiry,device_limit=data['devices']))
        await self.repo.set_setting(f'subscription_traffic_limit:{sub.id}',str(data['traffic']))
        if data.get('issuance_kind')=='replacement':
            await self.repo.set_setting(f'subscription_issuance_kind:{sub.id}','replacement')
        if data.get('widget_access'):
            if not self.settings.is_owner(actor.telegram_id):
                raise PermissionError('Выбор антиглушилки недоступен.')
            from app.services.bot_antig_test import validate_profile
            access=validate_profile(data['widget_access'])
            await self.repo.set_setting(f'bot_test_access:{sub.id}',json.dumps(access))
        from app.services.vless_provision import ensure
        credential=await ensure(self.repo,self.settings,sub,user,operation_key='bot-client-grant:'+token)
        if (not credential or credential.user_id != user.id or credential.subscription_id != sub.id
                or credential.status != 'active' or not credential.subscription_url):
            raise ValueError('Панель не подтвердила рабочий ключ.')
        key=await self.session.get(AccessKey,credential.access_key_id)
        if not key or key.user_id != user.id or key.status != 'active' or key.mode != 'vless':
            raise ValueError('Панель не подтвердила рабочий ключ.')
        key.name=data['name']
        await self.repo.set_setting(f'subscription_name:{sub.id}',data['name'])
        await self.repo.set_setting(f'subscription_duration:{sub.id}',data['label'])
        if data.get('custom_sale'):
            from app.services.bot_custom_sales import record
            payment=await record(self.repo,actor,user,sub,credential,token,data['custom_sale'])
            data['payment_id']=payment.id
        elif 'tariff_sale' in data:
            from app.web.admin_reports import record_sale
            payment = await self.repo.add(Payment(user_id=user.id, subscription_id=sub.id,
                amount=data['tariff_sale'], status='manual_paid', provider_id='bot-client-sale:'+token))
            await record_sale(self.repo, payment, tariff.months, sub.device_limit, tariff=tariff, source='manual')
            snapshot = json.loads(await self.repo.setting(f'sale_snapshot:{payment.id}'))
            snapshot.update(recorded_by=actor.id, key_id=key.id, issuance_kind='manual_sale')
            await self.repo.set_setting(f'sale_snapshot:{payment.id}', json.dumps(snapshot))
            data['payment_id'] = payment.id
            sub.kind = 'paid'
        if user.id != actor.id and user.telegram_id and user.telegram_id > 0:
            await self.repo.add(ChatNotice(dedup=f'client-grant:{credential.id}', target_id=user.telegram_id,
                                           kind='client_grant', text='issued' if data.get('payment_id') else 'gift'))
        from app.services.admin import AdminService
        await AdminService(self.repo,self.settings).audit(actor,'client_grant',user.id,f'key={key.id}; devices={data["devices"]}; term={data["label"]}'+('; issuance=replacement; no_sale' if data.get('issuance_kind')=='replacement' else ''))
        data['credential']=credential.id
        await self.repo.set_setting('client_grant:'+token,json.dumps(data))
        return await self.issued_screen(user,credential,actor,data)

    async def issued_screen(self,user,credential,actor,data):
        if data.get('tariff') and data.get('issuance_kind')!='replacement':
            screen = self.success(user,credential)
            if data.get('payment_id'):
                from app.services.screens import money
                screen = rich(screen.text+f'\n\nПродажа учтена. <b>{money(data["tariff_sale"])}</b>', screen.rows)
            return screen
        sub=await self.session.get(Subscription,credential.subscription_id)
        if not sub or sub.user_id!=user.id or credential.user_id!=user.id:
            raise ValueError('Выданная подписка недоступна.')
        # Reuse the actual key card, but do not treat a complimentary grant as
        # payment or invoke receipt-time provisioning of a proxy gift.
        from app.services.vless_provision import connection
        screen=await connection(self.repo,self.settings,sub,user,paid=False)
        if not screen:
            raise ValueError('Карточка выданного ключа недоступна.')
        screen.text=screen.text.replace('Индивидуальная подписка',escape(data['label']))
        screen.text='✅ <b>Подписка выдана</b>\n\n'+screen.text
        if data.get('issuance_kind')=='replacement':
            screen.text+='\n\n♻️ <b>Замена / без продажи.</b> Выручка и баланс не изменились.'
        if data.get('custom_sale'):
            from app.services.screens import money
            screen.text+='\n\n🧾 <b>Нестандартная продажа учтена: '+money(data['custom_sale']['total'])+'</b>'
        if actor.id!=user.id:
            screen.text='👤 Клиент: '+self.identity(user)+'\n\n'+screen.text
            # Customer callbacks authorize against their sender. An administrator
            # issuing to another customer must receive administrator controls.
            mapping={f'renew:{sub.id}':f'admin:renewcustom:devices:{sub.id}',
                f'devices:{sub.id}':f'admin:r:devices:{credential.id}',
                f'rename:{sub.id}':f'admin:r:input:name:{credential.id}',
                f'subscription_extra:{sub.id}':f'admin:r:key:{credential.id}',
                'subscriptions':f'admin:client:{user.id}'}
            rows=[];options={}
            for row in screen.rows:
                items=[]
                for label,target in row:
                    if target.startswith('autorenew:'):continue
                    mapped=mapping.get(target,target)
                    items.append((label,mapped))
                    if target in screen.button_options:options[mapped]=screen.button_options[target]
                if items:rows.append(items)
            screen.rows=rows;screen.button_options=options
        screen.appearance_page='receipt'
        screen.structured=True
        from app.services.screens import ScreenService
        return await ScreenService(self.repo,self.settings,None,None).decorate('receipt',actor,screen)

    def success(self,user,credential):
        return rich('✅ <b>Ключ выдан</b>\n\nКлиент: '+self.identity(user)+'\n\nКлюч доступен клиенту в разделе «Мои ключи».',[[('🔑 Открыть ключ',f'admin:r:key:{credential.id}')],[('👤 Карточка клиента',f'admin:client:{user.id}')]])

    async def render(self,action,actor):
        p=action.split(':')
        if p[1]=='clientgrant':
            if p[2]=='apply':return await self.apply(p[3],actor)
            if p[2]=='free':return await self.apply(p[3],actor,complimentary=True)
            uid = int(p[3])
            user = await self.customer(uid, actor)
            if p[2]=='tariff':
                from app.services.bot_antig_price import enabled, tariff_total
                from app.services.bot_catalog import available, devices_label
                from app.services.screens import money
                tariff = next((item for item in await available(self.repo) if item.id == int(p[4])), None)
                if not tariff:
                    raise ValueError('Тариф больше недоступен. Выберите другой.')
                if len(p) == 5 and await enabled(self.repo):
                    surcharge = await tariff_total(self.repo, tariff)
                    return rich('🛡 <b>Добавить антиглушилку?</b>\n\nКлиент: '+self.identity(user)
                                +f'\n\n{devices_label(tariff.device_limit)} · {duration(tariff.months)}'
                                +'\nАнтиглушилка действует весь срок подписки.',
                                [[('С антиглушилкой · +'+money(surcharge), action+':yes')],
                                 [('Только VPN', action+':no')],
                                 [('← Выбрать срок', f'admin:clientgrant:devices:{uid}:{tariff.device_limit}')]])
                return await self.preview(uid,actor,tariff_id=tariff.id,card_tariff=True,antig=p[-1]=='yes')
            if p[2]!='devices':
                raise ValueError('Действие недоступно.')
            from app.services.bot_catalog import available, devices_label
            from app.services.catalog import public_price
            from app.services.screens import money
            devices = int(p[4])
            tariffs = sorted((item for item in await available(self.repo) if item.device_limit == devices),
                             key=lambda item: (item.months, item.id))
            offset = max(0, int(p[5])) if len(p)>5 else 0
            rows = []
            for item in tariffs[offset:offset+10]:
                amount = await public_price(self.repo, 'vpn', item.id, item.price)
                rows.append([(f'{duration(item.months)} · {money(amount)}', f'admin:clientgrant:tariff:{uid}:{item.id}')])
            page = f'admin:clientgrant:devices:{uid}:{devices}'
            if offset:
                rows.append([('← Предыдущие', f'{page}:{max(0,offset-10)}')])
            if offset+10<len(tariffs):
                rows.append([('Следующие →', f'{page}:{offset+10}')])
            rows.append([('← Выбрать устройства', f'admin:issue:{uid}')])
            return rich('📅 <b>На какой срок?</b>\n\nКлиент: '+self.identity(user)
                        +'\n'+devices_label(devices), rows)
        uid=int(p[2])
        user=await self.customer(uid,actor)
        from app.services.bot_catalog import available, devices_label
        devices = sorted({tariff.device_limit for tariff in await available(self.repo)})
        rows = [[(devices_label(count), f'admin:clientgrant:devices:{uid}:{count}')] for count in devices]
        rows += [[('⚙️ Свой срок и устройства',f'admin:grantconfig:new:{uid}:card')],[('← Карточка клиента',f'admin:client:{uid}')]]
        return rich('🎁 <b>Выдать ключ</b>\n\nКлиент: '+self.identity(user)
                    +'\n\nВыберите количество устройств, затем срок подписки.',rows)
