"""Telegram custom grants with explicit manual-sale or complimentary intent."""
import json
import re
import secrets
from html import escape

from sqlalchemy import select

from app.database.models import AppSetting, now
from app.services.admin_client_grant import ClientGrant, rich
from app.services.screens import duration

def parse_sale(value):
    parts=value.strip().split(',')
    if len(parts)==4:
        if parts[2].strip().casefold() not in ('антиглушилка','без антиглушилки','+','-'):raise ValueError('Третьим значением укажите + для антиглушилки или - без неё.')
        parts=parts[:2]+[parts[3]]
    if len(parts) not in (2,3):raise ValueError('Формат: 2, 3 месяца, 700. Без цены — бесплатно.')
    parsed=parse_combined(','.join(parts[:2]))
    from app.services.bot_sale_price import rubles
    total=0 if len(parts)==2 or parts[2].strip().casefold() in ('','бесплатно','подарок') else rubles(parts[2],allow_zero=True)
    return parsed,total


def parse_period(value):
    match = re.fullmatch(
        r'\s*(\d+)\s*(д|дн\.?|день|дня|дней|дни|ч|час|часа|часов|h|d|мес\.?|месяц|месяца|месяцев)?\s*',
        value, re.I,
    )
    if not match:
        raise ValueError('Введите срок: 60 дней, 12 часов или 2 месяца.')
    amount, unit = int(match[1]), (match[2] or 'дней').lower()
    kind = 'months' if unit.startswith(('мес', 'меся')) else 'hours' if unit in ('ч', 'час', 'часа', 'часов', 'h') else 'days'
    maximum = {'days': 3650, 'hours': 87600, 'months': 120}[kind]
    if not 1 <= amount <= maximum:
        raise ValueError('Срок: 1–3650 дней, 1–87600 часов или 1–120 месяцев.')
    return kind, amount


def period_label(data):
    amount = data['amount']
    return duration(amount) if data['unit'] == 'months' else f'{amount} '+('ч.' if data['unit'] == 'hours' else 'дн.')


def parse_combined(value, *, proxy=False):
    """Devices first, explicit comma separator; never guess reversed operands."""
    match = re.fullmatch(
        r'\s*(\d{1,3})\s*(?:устройство|устройства|устройств|подключение|подключения|подключений|устр\.?)?\s*,\s*([^,\n]{1,48})\s*',
        value, re.I,
    )
    if not match or not 1 <= int(match[1]) <= 100:
        raise ValueError('Укажите сначала подключения (1–100), затем срок через запятую. Например: 2, 60 дней.')
    unit, amount = parse_period(match[2])
    if proxy and unit != 'days':
        raise ValueError('Для прокси укажите срок в днях. Например: 2, 60 дней.')
    return dict(devices=int(match[1]), unit=unit, amount=amount)


class BotGrantConfig:
    def __init__(self, repo, settings):
        self.repo, self.settings = repo, settings
        self.grants = ClientGrant(repo, settings)

    async def replacement(self, uid, actor, offset=0):
        user = await self.grants.customer(uid, actor)
        if user.id != actor.id:
            raise ValueError('Откройте замену из своего раздела «Купить подписку».')
        tariffs = [t for t in await self.repo.tariffs()
            if await self.repo.tariff_setting(t, 'tariff_enabled', '1') == '1'
            and await self.repo.tariff_setting(t, 'tariff_deleted', '0') != '1']
        tariffs.sort(key=lambda t: (t.device_limit, t.months, t.id))
        offset = min(max(0, offset), max(0, (len(tariffs)-1)//5*5))
        rows = [[(f'{t.device_limit} устр. · {duration(t.months)}',
            f'admin:grantconfig:replace_tariff:{uid}:{t.id}')] for t in tariffs[offset:offset+5]]
        if offset:
            rows.append([('← Предыдущие', f'admin:grantconfig:replacement:{uid}:{offset-5}')])
        if offset+5 < len(tariffs):
            rows.append([('Следующие →', f'admin:grantconfig:replacement:{uid}:{offset+5}')])
        rows += [[('⚙️ Свой срок и устройства', f'admin:grantconfig:new:{uid}:replacement')],
                 [('← Купить подписку', 'buy')]]
        return rich('♻️ <b>Замена / выдача без продажи</b>\n\nВыберите тариф или задайте свои параметры.'
            '\n\nКлюч выдаётся без оплаты, выручки и начисления на баланс. В продажах не учитывается.'
            '\n\nЭто новый ключ. Старый ключ автоматически не отключается.', rows)

    async def draft(self, token, actor):
        if not re.fullmatch(r'[0-9a-f]{16}', token):
            raise ValueError('Настройка недоступна. Откройте новую выдачу.')
        row = await self.repo.session.scalar(
            select(AppSetting).where(AppSetting.key == 'bot_grant_draft:'+token).with_for_update()
        )
        data = json.loads(row.value) if row else {}
        if data.get('actor') != actor.telegram_id:
            raise PermissionError('Настройка другого администратора.')
        await self.grants.customer(data['user'], actor)
        if data.get('expires', 0) < now().timestamp():
            raise ValueError('Настройка устарела. Откройте новую выдачу.')
        return data

    async def save(self, token, data):
        await self.repo.set_setting('bot_grant_draft:'+token, json.dumps(data))

    async def invalidate(self, data):
        token = data.get('confirmation')
        if not token:
            return
        row = await self.repo.session.scalar(
            select(AppSetting).where(AppSetting.key == 'client_grant:'+token)
            .with_for_update().execution_options(populate_existing=True)
        )
        stored = json.loads(row.value) if row else {}
        if stored.get('credential'):
            raise ValueError('Ключ уже выдан. Для ещё одного ключа начните новую выдачу.')
        stored['expires'] = 0
        await self.repo.set_setting('client_grant:'+token, json.dumps(stored))
        data.pop('confirmation', None)

    async def view(self, token, data, actor):
        return await self.prompt(token, 'all', actor)

    async def legacy_view(self, token, data, actor):
        user = await self.grants.customer(data['user'], actor)
        base = 'admin:grantconfig:'
        back = (f'admin:grantconfig:replacement:{user.id}' if data['origin'] == 'replacement'
                    else 'buy' if data['origin'] == 'buy' else f'admin:issue:{user.id}')
        text = (
            '⚙️ <b>Нестандартная подписка</b>\n\nКлиент: '+self.grants.identity(user)+
            f'\n\n<b>Срок:</b> {period_label(data)}\n<b>Устройства:</b> {data["devices"]}'
            f'\n<b>Название:</b> {escape(data["name"] or "По параметрам подписки")}'
            '\n\nВыдача бесплатная: без списания с баланса и без дохода от продажи.'
            '\n\nИзмените параметры, затем проверьте их перед выдачей. Ключ получит выбранный клиент.'
        )
        return rich(text, [
            [('📱 Количество устройств', base+'devices:'+token)],
            [('🗓 Срок подписки', base+'period:'+token)],
            [('✏️ Название ключа', f'admin:input:grantconfig:{token}:name')],
            [('✅ Проверить и выдать', base+'review:'+token)],
            [('← Назад', back)],
        ])

    async def prompt(self, token, field, actor):
        data = await self.draft(token, actor)
        if field == 'all':
            user = await self.grants.customer(data['user'], actor)
            back = (f'admin:grantconfig:replacement:{user.id}' if data['origin'] == 'replacement'
                    else 'buy' if data['origin'] == 'buy' else f'admin:issue:{user.id}')
            replacement = data['origin'] == 'replacement'
            note = ('\n\nЗамена: без оплаты, выручки и начисления на баланс. В продажах не учитывается.' if replacement else
                '\n\nУкажите цену третьим значением: <code>2, 3 месяца, 700</code> — 700 ₽ за всю подписку.'
                '\n\nБез цены или с ценой 0 — бесплатно, без учёта продажи.')
            screen = rich(('♻️ <b>Замена: свои параметры</b>' if replacement else '⚙️ <b>Другой вариант подписки</b>')+'\n\nКлиент: '+self.grants.identity(user)+
                '\n\nОтправьте одним сообщением количество устройств и срок через запятую:'
                '\n\n<code>2 устройства, 60 дней</code>\n<code>1, 12 часов</code>\n<code>3, 2 месяца</code>'
                +note+('\n\n🛡 С антиглушилкой на тот же срок: <code>1, 1 месяц, +, 250</code>. Последнее число — итоговая цена. Для бесплатной выдачи укажите 0. Без дополнения: <code>1, 1 месяц, -, 150</code>.' if data['origin'] in ('buy','card') else ''), [[('← Назад', back)]])
            screen.admin_input_action = f'admin:input:grantconfig:{token}:all'
            return screen
        if field not in ('devices', 'period', 'name'):
            raise ValueError('Настройка недоступна.')
        current = str(data['devices']) if field == 'devices' else period_label(data) if field == 'period' else data['name'] or 'По параметрам подписки'
        prompt = {
            'devices': 'Введите число устройств от 1 до 100.\n\nНапример: <code>2</code>',
            'period': 'Введите срок в днях, часах или календарных месяцах.\n\n<code>60 дней</code>\n<code>12 часов</code>\n<code>2 месяца</code>',
            'name': 'Введите название ключа до 80 символов.\n\nНапример: <code>Рабочий</code>\n\nЧтобы оставить название по параметрам, отправьте <code>-</code>.',
        }[field]
        return rich('<b>Изменить параметр подписки</b>\n\nСейчас: '+escape(current)+'\n\n'+prompt,
                    [[('← К настройке', 'admin:grantconfig:view:'+token)]])

    async def input(self, token, field, value, actor):
        data = await self.draft(token, actor)
        value = value.strip()
        if field == 'all':
            changes,total = parse_sale(value)
            fields=value.split(',')
            if len(fields)==4:
                if not self.settings.is_owner(actor.telegram_id) or data['origin'] not in ('buy','card'):
                    raise PermissionError('Дополнение доступно в администраторской выдаче.')
                if fields[2].strip().casefold()in ('антиглушилка','+'):
                    from app.services.bot_antig_test import resolve
                    if data['origin']=='card':
                        from app.services.bot_antig_price import require_enabled
                        await require_enabled(self.repo)
                    changes['widget_access']=await resolve(self.repo,self.settings,True)
                else:data.pop('widget_access',None)
            elif data['origin']=='card' and not data.get('widget_prefill'):
                data.pop('widget_access',None)
            if data['origin']=='replacement' and total:raise ValueError('Замена бесплатная. Уберите цену.')
            changes.update(account_sale=total>0,explicit_price=total)
        elif field == 'devices':
            if not re.fullmatch(r'[0-9]{1,3}', value) or not 1 <= int(value) <= 100:
                raise ValueError('Введите целое число устройств от 1 до 100.')
            changes = dict(devices=int(value))
        elif field == 'period':
            unit, amount = parse_period(value)
            changes = dict(unit=unit, amount=amount)
        elif field == 'name':
            if not 1 <= len(value) <= 80 or any(ord(char) < 32 for char in value):
                raise ValueError('Название: от 1 до 80 символов без переносов строк.')
            changes = dict(name='' if value == '-' else value)
        else:
            raise ValueError('Настройка недоступна.')
        await self.invalidate(data)
        data.update(changes)
        await self.save(token, data)
        if field == 'all':
            return await self.render('admin:grantconfig:review:'+token, actor)
        return await self.view(token, data, actor)

    async def render(self, action, actor):
        parts = action.split(':')
        operation = parts[2]
        if operation == 'replacement':
            return await self.replacement(int(parts[3]), actor, int(parts[4]) if len(parts)>4 else 0)
        if operation == 'replace_tariff':
            uid = int(parts[3])
            user = await self.grants.customer(uid, actor)
            if user.id != actor.id:
                raise ValueError('Получатель недоступен.')
            return await self.grants.preview(uid, actor, tariff_id=int(parts[4]),
                issuance_kind='replacement', return_to=f'admin:grantconfig:replacement:{uid}')
        if operation == 'new':
            user = await self.grants.customer(int(parts[3]), actor)
            origin = parts[4] if len(parts) == 5 else 'card'
            if origin not in ('buy', 'card', 'replacement') or origin in ('buy', 'replacement') and user.id != actor.id:
                raise ValueError('Получатель недоступен.')
            token = secrets.token_hex(8)
            data = dict(actor=actor.telegram_id, user=user.id, origin=origin, unit='days', amount=30,
                        devices=1, name='', expires=now().timestamp()+1800)
            await self.save(token, data)
            return await self.view(token, data, actor)
        token = parts[3]
        data = await self.draft(token, actor)
        if operation == 'view':
            return await self.view(token, data, actor)
        if operation in ('devices', 'period'):
            values = [('1 устройство', '1'), ('2 устройства', '2'), ('3 устройства', '3'), ('5 устройств', '5')] if operation == 'devices' else [
                ('7 дней', '7 дней'), ('30 дней', '30 дней'), ('60 дней', '60 дней'), ('2 месяца', '2 месяца'), ('3 месяца', '3 месяца')]
            rows = [[(label, f'admin:grantconfig:set:{token}:{operation}:{index}')] for index, (label, _) in enumerate(values)]
            rows += [[('✍️ Ввести другое значение', f'admin:input:grantconfig:{token}:{operation}')],
                     [('← К настройке', 'admin:grantconfig:view:'+token)]]
            return rich('<b>'+('Количество устройств' if operation == 'devices' else 'Срок подписки')+'</b>\n\nКлиент уже выбран. Изменится только параметр этой выдачи.', rows)
        if operation == 'set':
            field, index = parts[4], int(parts[5])
            values = ['1', '2', '3', '5'] if field == 'devices' else ['7 дней', '30 дней', '60 дней', '2 месяца', '3 месяца'] if field == 'period' else []
            if not 0 <= index < len(values):
                raise ValueError('Значение недоступно.')
            return await self.input(token, field, values[index], actor)
        if operation == 'review':
            user = await self.grants.customer(data['user'], actor)
            if data.get('confirmation'):
                stored = json.loads(await self.repo.setting('client_grant:'+data['confirmation'], '{}'))
                if stored.get('credential'):
                    return await self.grants.apply(data['confirmation'], actor)
                if stored.get('expires', 0) > now().timestamp():
                    return self.grants.confirmation(user, stored, data['confirmation'])
            hours = data['amount'] * 24 if data['unit'] == 'days' else data['amount']
            screen = await self.grants.preview(data['user'], actor, hours=hours, devices=data['devices'],
                custom_months=data['amount'] if data['unit'] == 'months' else None,
                name=data['name'], label=period_label(data), return_to='admin:grantconfig:view:'+token,
                account_sale=False if data['origin']=='replacement' else data.get('account_sale',False),
                explicit_price=data.get('explicit_price'),
                issuance_kind='replacement' if data['origin']=='replacement' else None,
                widget_access=data.get('widget_access'))
            data['confirmation'] = next(target.rsplit(':', 1)[1] for row in screen.rows for _, target in row if target.startswith('admin:clientgrant:apply:'))
            await self.save(token, data)
            return screen
        raise ValueError('Действие недоступно.')
