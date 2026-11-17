"""Bot-only Antiglushilka surcharge settings; never edits existing sales."""
import json
import re
import secrets

from sqlalchemy import text

from app.database.models import AppSetting, AdminAudit, now
from app.services.admin_client_grant import rich
from app.services.screens import money

KEY = 'bot_antig_monthly_price'
ENABLED_KEY = 'bot_antig_enabled'
DEFAULT = 10000
MAXIMUM = 100000000


async def enabled(repo):
    # Missing/invalid setting is deliberately OFF. Refresh on each decision so
    # old keyboards, cached ORM settings and payment links cannot enable sales.
    row = await repo.session.get(AppSetting, ENABLED_KEY, populate_existing=True)
    return bool(row and row.value == '1')


async def require_enabled(repo):
    if not await enabled(repo):
        raise ValueError('Антиглушилка сейчас выключена. Выберите обычную подписку.')


async def monthly(repo, *, fresh=False):
    if fresh:
        row = await repo.session.get(AppSetting, KEY, populate_existing=True)
        raw = row.value if row else str(DEFAULT)
    else:
        raw = await repo.setting(KEY, str(DEFAULT))
    if not isinstance(raw, str) or not re.fullmatch(r'[0-9]{1,9}', raw) or not 0 <= int(raw) <= MAXIMUM:
        raise ValueError('Некорректная цена антиглушилки. Проверьте настройки в админке.')
    return int(raw)


async def surcharge(repo, months):
    if type(months) is not int or not 1 <= months <= 120:
        raise ValueError('Некорректный срок подписки.')
    amount = await monthly(repo) * months
    if amount > MAXIMUM:
        raise ValueError('Доплата за выбранный срок слишком велика. Проверьте цену антиглушилки.')
    return amount


async def tariff_total(repo, tariff, *, discounted=True):
    await repo.session.flush()
    row=await repo.session.get(AppSetting, f'bot_antig_tariff_price:{tariff.id}', populate_existing=True)
    if row is None:
        base = await surcharge(repo,tariff.months)
    elif not re.fullmatch(r'[0-9]{1,9}',row.value or '') or not 0<=int(row.value)<=MAXIMUM:
        raise ValueError('Некорректная доплата этого тарифа. Проверьте настройки антиглушилки.')
    else:
        base = int(row.value)
    from app.services.timed_discount import apply_price
    return await apply_price(repo,'antig',base) if discounted else base


class AntigPrice:
    def __init__(self, repo, settings):
        self.repo, self.settings = repo, settings

    def require(self, actor):
        if not self.settings.is_owner(actor.telegram_id):
            raise PermissionError('Цена антиглушилки доступна только администратору.')

    async def render(self, action, actor):
        self.require(actor)
        p = action.split(':')
        if p == ['admin', 'antig']:
            rate = await monthly(self.repo)
            active = await enabled(self.repo)
            toggle = 'admin:antig:visibility:'+('1:0' if active else '0:1')
            result = rich('🛡️ <b>Антиглушилка</b>\n\n'
                '<b>В покупке:</b> '+('включена' if active else 'выключена')+'\n\n'
                '<b>Базовая доплата за месяц:</b> '+money(rate)+'\n\n'
                '\n\nВ разделе «Цены по тарифам» можно задать отдельную доплату для каждого срока и количества устройств. Для тарифов без своей цены используется базовая доплата.'
                '\n\nПри выключении блок скрыт и новые оплаты с антиглушилкой недоступны. Уже оплаченные подписки и начатые банковские счета сохраняют условия.'
                '\n\nПосле изменения откройте покупку заново. Новая цена применяется к новым оформлениям.',
                [[('🔴 Выключить' if active else '🟢 Включить',toggle)],
                 [('💰 Цены по тарифам', 'admin:antig:prices:0')], [('← Назад', 'admin:commerce')]])
            result.button_options[toggle]={'style':'danger' if active else 'success'}
            return result
        if len(p)==4 and p[2]=='prices':
            from app.services.bot_catalog import available,devices_label
            tariffs=sorted(await available(self.repo),key=lambda t:(t.device_limit,t.months,t.id))
            offset=int(p[3]);offset=max(0,min(offset,max(0,(len(tariffs)-1)//5*5)))
            rows=[]
            for t in tariffs[offset:offset+5]:
                label=devices_label(t.device_limit)+' · '+str(t.months)+' мес. · +'+money(await tariff_total(self.repo,t,discounted=False))
                rows.append([(label,f'admin:input:antig_price:{t.id}')])
            if offset:rows.append([('← Предыдущие',f'admin:antig:prices:{offset-5}')])
            if offset+5<len(tariffs):rows.append([('Следующие →',f'admin:antig:prices:{offset+5}')])
            rows.append([('← Антиглушилка','admin:antig')])
            return rich('💰 <b>Доплата по тарифам</b>\n\nВыберите тариф. Цена указана за весь выбранный срок, а не за месяц.\n\nНовые цены применяются к новым оформлениям. Уже созданные счета сохраняют сумму.',rows)
        if len(p)==5 and p[2]=='visibility' and p[3] in ('0','1') and p[4] in ('0','1') and p[3]!=p[4]:
            if self.repo.session.bind.dialect.name == 'postgresql':
                await self.repo.session.execute(text("SELECT pg_advisory_xact_lock(hashtext('bot_antig_enabled'))"))
            current = await enabled(self.repo)
            desired = p[4]=='1'
            if current != desired:
                if current != (p[3]=='1'):
                    raise ValueError('Состояние изменилось. Откройте раздел заново.')
                await self.repo.set_setting(ENABLED_KEY,p[4])
                await self.repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='antig_visibility_change',
                    target=ENABLED_KEY,details='enabled' if desired else 'disabled'))
            return await self.render('admin:antig',actor)
        if len(p) == 4 and p[2] == 'apply':
            return await self.apply(p[3], actor)
        raise ValueError('Настройка недоступна.')

    async def prompt(self, actor, tid=None):
        self.require(actor)
        if tid is not None:
            from app.services.bot_catalog import available,devices_label
            t=next((t for t in await available(self.repo) if t.id==tid),None)
            if not t:raise ValueError('Тариф недоступен.')
            return rich('💰 <b>Доплата за антиглушилку</b>\n\n'+devices_label(t.device_limit)+' · '+str(t.months)+' мес.\n\nБазовая цена: <b>'+money(await tariff_total(self.repo,t,discounted=False))+'</b>\n\nОтправьте доплату в рублях за весь этот срок. Например: <code>150</code> или <code>99,50</code>. Ноль — без доплаты.',[[('← Отмена','admin:antig:prices:0')]])
        return rich('✏️ <b>Цена антиглушилки за месяц</b>\n\nСейчас: <b>'+money(await monthly(self.repo))+'</b>'
            '\n\nОтправьте новую доплату в рублях. Например: <code>150</code> или <code>99,50</code>.'
            '\n\nЗа несколько месяцев сумма умножается на срок. Значение <code>0</code> — без доплаты.',
            [[('← Отмена', 'admin:antig')]])

    async def input(self, value, actor, tid=None):
        self.require(actor)
        from app.services.bot_sale_price import rubles
        amount = rubles(value, allow_zero=True)
        if tid is not None:
            from app.services.bot_catalog import available
            t=next((t for t in await available(self.repo) if t.id==tid),None)
            if not t:raise ValueError('Тариф недоступен.')
            previous=await tariff_total(self.repo,t,discounted=False)
        else:previous = await monthly(self.repo, fresh=True)
        token = secrets.token_hex(10)
        await self.repo.set_setting('antig_price_change:'+token, json.dumps(dict(
            actor=actor.id, tariff=tid, old=previous, new=amount, expires=now().timestamp()+600)))
        result = rich('🛡️ <b>Сохранить новую цену?</b>\n\n'+('Доплата за весь срок тарифа #'+str(tid) if tid else 'Доплата за месяц')+':\n'
            +money(previous)+' → <b>'+money(amount)+'</b>\n\nУже оформленные выдачи не изменятся.',
            [[('✅ Сохранить цену', 'admin:antig:apply:'+token)], [('✏️ Изменить', 'admin:input:antig_price'+(':'+str(tid) if tid else ''))], [('← Отмена', 'admin:antig')]])
        result.button_options['admin:antig:apply:'+token] = {'style': 'success'}
        return result

    async def apply(self, token, actor):
        self.require(actor)
        if not re.fullmatch(r'[0-9a-f]{20}', token):
            raise ValueError('Подтверждение недоступно.')
        row = await self.repo.session.get(AppSetting, 'antig_price_change:'+token, with_for_update=True, populate_existing=True)
        data = json.loads(row.value) if row else {}
        if data.get('actor') != actor.id:
            raise PermissionError('Подтверждение другого администратора.')
        if data.get('done'):
            return rich('✅ Цена уже сохранена.', [[('← Настройки антиглушилки', 'admin:antig')]])
        if data.get('expires', 0) < now().timestamp():
            raise ValueError('Подтверждение устарело. Укажите цену заново.')
        if self.repo.session.bind.dialect.name == 'postgresql':
            await self.repo.session.execute(text("SELECT pg_advisory_xact_lock(hashtext('bot_antig_monthly_price'))"))
        tid=data.get('tariff')
        if tid is not None:
            from app.services.bot_catalog import available
            t=next((t for t in await available(self.repo) if t.id==tid),None)
            if not t:raise ValueError('Тариф недоступен.')
            current=await tariff_total(self.repo,t,discounted=False)
        else:current=await monthly(self.repo,fresh=True)
        if current != data['old']:
            raise ValueError('Цена уже изменена другим администратором. Откройте настройки заново.')
        if type(data.get('new')) is not int or not 0 <= data['new'] <= MAXIMUM:
            raise ValueError('Некорректная цена.')
        price_key=f'bot_antig_tariff_price:{tid}' if tid else KEY
        await self.repo.set_setting(price_key, str(data['new']))
        data['done'] = True
        row.value = json.dumps(data)
        await self.repo.add(AdminAudit(actor_telegram_id=actor.telegram_id, action='antig_price_change', target=price_key,
            details=f"Surcharge (kopecks): {data['old']} -> {data['new']}; existing sales unchanged"))
        return rich('✅ <b>Цена сохранена</b>\n\n'+('Доплата за весь срок тарифа #'+str(tid) if tid else 'Доплата за месяц')+': <b>'+money(data['new'])+'</b>.',
            [[('← Настройки антиглушилки', 'admin:antig')]])
