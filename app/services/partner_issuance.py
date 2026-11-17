"""Confirm partner tariff terms and record the debt when the working key is issued."""
import json
import re
import secrets
from datetime import datetime
from html import escape
from types import SimpleNamespace

from sqlalchemy import select

from app.database.models import AccessKey, AppSetting, Subscription, Tariff, now
from app.services.admin_client_grant import rich
from app.services.billing import extend, purchase_limits
from app.services.bot_catalog import devices_label
from app.services.partner_ledger import PRICE, append, entries, lock, total
from app.services.remnawave import RemnawaveApiError
from app.services.screens import duration, money


QUOTE = 'partner_issue:'
PLAN = 'partner_plan:'


async def catalogue_tariff(repo, devices, months):
    tariffs = await repo.session.scalars(select(Tariff).where(
        Tariff.device_limit == devices, Tariff.months == months,
    ).order_by(Tariff.id))
    for tariff in tariffs:
        if (await repo.tariff_setting(tariff, 'tariff_enabled', '1') == '1'
                and await repo.tariff_setting(tariff, 'tariff_deleted', '0') != '1'):
            return tariff
    return None


async def addon_price(repo, months, tariff=None):
    from app.services.bot_antig_price import surcharge, tariff_total

    regular = await tariff_total(repo, tariff, discounted=False) if tariff else await surcharge(repo, months)
    return (regular + 1) // 2


async def draft(repo, settings, user, devices, months, anti, tariff):
    from app.services.bot_antig_price import require_enabled
    from app.services.bot_antig_test import resolve

    access = None
    extra = 0
    if anti:
        await require_enabled(repo)
        extra = await addon_price(repo, months, tariff)
        access = await resolve(repo, settings, True)
    base = PRICE[months][devices]
    _, traffic = await purchase_limits(repo, tariff or SimpleNamespace(device_limit=devices),
                                       None if tariff else {'traffic_bytes': 0})
    stamp = now()
    token = secrets.token_hex(10)
    await repo.set_setting(QUOTE + token, json.dumps(dict(
        actor=user.id, devices=devices, months=months, base=base, antig=extra, access=access,
        tariff=tariff.id if tariff else None, traffic=traffic, until=stamp.timestamp()+900,
        expiry=extend(stamp, months).isoformat(),
    )))
    target = f'partner:issue:{token}'
    text = f'🔑 <b>Выдать партнёрский ключ</b>\n\n{devices_label(devices)} · {duration(months)}\nVPN {money(base)}'
    if anti:
        text += f'\nАнтиглушилка {money(extra)}'
    text += f'\n\nК начислению в долг <b>{money(base+extra)}</b>'
    screen = rich(text, [[('Выдать и записать в долг', target)],
                         [('← Назад', f'partner:plan:{devices}:{months}')]])
    screen.button_options[target] = {'style': 'success'}
    return screen


async def issue(repo, settings, user, token):
    from app.services.bot_antig_price import require_enabled
    from app.services.subscription_labels import refresh
    from app.services.vless_creation import unresolved
    from app.services.vless_provision import ensure

    if not re.fullmatch(r'[0-9a-f]{20}', token):
        raise ValueError('Подтверждение недоступно.')
    await lock(repo, user.id)
    from app.services.partner_bot import PartnerService
    await PartnerService(repo, settings).authorize(user)
    row = await repo.session.get(AppSetting, QUOTE+token, populate_existing=True)
    terms = json.loads(row.value) if row else {}
    if terms.get('actor') != user.id:
        raise PermissionError('Подтверждение другого партнёра.')
    operation = 'partner-issue:' + token
    if not terms.get('result'):
        recovering = await unresolved(repo, user, [operation])
        if not recovering:
            if now().timestamp() > terms['until']:
                raise ValueError('Подтверждение устарело. Выберите тариф заново.')
            if terms['access']:
                await require_enabled(repo)
        async with repo.session.begin_nested():
            sub = await repo.add(Subscription(
                user_id=user.id, kind='partner', tariff_id=terms['tariff'],
                expires_at=datetime.fromisoformat(terms['expiry']), device_limit=terms['devices'],
            ))
            await repo.set_setting(f'subscription_traffic_limit:{sub.id}', str(terms['traffic']))
            if terms['access']:
                await repo.set_setting(f'bot_test_access:{sub.id}', json.dumps(terms['access']))
            credential = await ensure(repo, settings, sub, user, operation_key=operation)
            key = await repo.session.get(AccessKey, credential.access_key_id) if credential else None
            if not key or key.mode != 'vless' or key.status != 'active' or credential.status != 'active':
                raise ValueError('Панель не подтвердила рабочий ключ. Долг не начислен.')
            await refresh(repo, sub, key, duration(terms['months']))
            await repo.set_setting(PLAN+str(sub.id), json.dumps(dict(months=terms['months'], devices=terms['devices'])))
            await append(repo, user.id, operation, 'issue', terms['base']+terms['antig'], user.id,
                         sid=sub.id, key_id=key.id, base=terms['base'], antig=terms['antig'],
                         months=terms['months'], devices=terms['devices'])
            await repo.set_setting(f'partner_issue_notice:{sub.id}', json.dumps({
                'charged': terms['base'] + terms['antig'], 'debt': total(await entries(repo, user.id)),
            }))
            terms['result'] = sub.id
            await repo.set_setting(QUOTE+token, json.dumps(terms))
    return terms


async def render(repo, settings, user, p):
    from app.services.bot_antig_price import enabled
    from app.services.vless_provision import connection

    op = p[0]
    if op == 'plans':
        devices = sorted({count for rates in PRICE.values() for count in rates})
        return rich('🔑 <b>Получить ключ</b>\n\nВыберите количество устройств. '
                    'Закупочная цена будет начислена в долг после выдачи.',
                    [[(devices_label(count), f'partner:terms:{count}')] for count in devices]
                    + [[('← Назад', 'partner:home')]])
    if op == 'issue':
        try:
            terms = await issue(repo, settings, user, p[1])
        except RemnawaveApiError:
            return rich('Панель пока не подтвердила выдачу. Повторите это подтверждение.',
                        [[('Повторить', f'partner:issue:{p[1]}')], [('← Назад', 'partner:plans')]])
        except ValueError as error:
            return rich(escape(str(error)), [[('Повторить', f'partner:issue:{p[1]}')], [('← Назад', 'partner:plans')]])
        sub = await repo.subscription(user.id, terms['result'])
        debt = total(await entries(repo, user.id))
        summary = (f'✅ <b>Ключ выдан</b>\n\nНачислено <b>{money(terms["base"]+terms["antig"])}</b>\n'
                   f'Текущий долг <b>{money(debt)}</b>')
        screen = await connection(repo, settings, sub, user) if sub else None
        if not screen:
            return rich(summary, [[('💼 Расчёты', 'partner:ledger')], [('← Назад', 'partner:home')]])
        screen.text = summary + '\n\n' + screen.text
        screen.rows = [row for row in screen.rows if not any(
            target.startswith(('renew:', 'autorenew:', 'up:', 'subscription_extra:')) for _, target in row
        )]
        screen.rows[-1:-1] = [[('💼 Расчёты', 'partner:ledger')]]
        return screen
    devices = int(p[1])
    if op == 'terms':
        plans = [(months, rates[devices]) for months, rates in PRICE.items() if devices in rates]
        if not plans:
            raise ValueError('Тариф недоступен.')
        rows = [[(f'{duration(months)} · {money(price)}', f'partner:plan:{devices}:{months}')]
                for months, price in plans]
        body = '\n'.join(f'{duration(months)} · {money(price)}' for months, price in plans)
        return rich(f'📅 <b>На какой срок?</b>\n\n{devices_label(devices)}\n\n'+body,
                    rows + [[('← Назад', 'partner:plans')]])
    months = int(p[2])
    base = PRICE.get(months, {}).get(devices)
    if base is None:
        raise ValueError('Тариф недоступен.')
    tariff = await catalogue_tariff(repo, devices, months)
    if op == 'quote':
        if p[3] not in ('0', '1'):
            raise ValueError('Выберите условия выдачи.')
        return await draft(repo, settings, user, devices, months, p[3]=='1', tariff)
    if not await enabled(repo):
        return await draft(repo, settings, user, devices, months, False, tariff)
    extra = await addon_price(repo, months, tariff)
    from app.services.bot_antig_test import choice_screen
    return choice_screen(f'{devices_label(devices)} · {duration(months)}',extra,
                         f'partner:quote:{devices}:{months}:1',f'partner:quote:{devices}:{months}:0',
                         f'partner:terms:{devices}',base=base,partner=True)
