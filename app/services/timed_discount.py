"""Time-limited retail discounts shared by Telegram and the website."""
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.database.models import AdminAudit, now

SCOPES = {'vpn': 'VPN-ключ', 'antig': 'Антиглушилка', 'both': 'VPN и антиглушилка'}
MOSCOW = ZoneInfo('Europe/Moscow')


def key(scope):
    return 'retail_timed_discount:' + scope


async def active(repo, scope):
    if scope not in ('vpn', 'antig'):
        return None
    try:
        value = json.loads(await repo.setting(key(scope), '{}'))
        end = datetime.fromisoformat(value['ends_at'])
        percent = value['percent']
        if end > now() and type(percent) is int and 1 <= percent <= 99:
            return percent, end
    except (ValueError, TypeError, KeyError):
        pass
    return None


async def apply_price(repo, scope, price):
    discount = await active(repo, scope)
    return max(1, price * (100 - discount[0]) // 100) if discount and price > 0 else price


def require(settings, actor):
    if not settings.is_owner(actor.telegram_id):
        raise PermissionError('Скидки доступны администратору.')


async def save(repo, actor, scope, percent=None, end=None):
    from app.services.customer_identity import lock_nonce
    if scope not in SCOPES:
        raise ValueError('Выберите, на что действует скидка.')
    await lock_nonce(repo.session, 'retail_timed_discount')
    value = json.dumps(dict(percent=percent, ends_at=end.isoformat())) if end else '{}'
    for target in ('vpn', 'antig') if scope == 'both' else (scope,):
        await repo.set_setting(key(target), value)
    await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id, action='retail_timed_discount',
                              target=scope, details=value))


async def menu(repo):
    from app.services.admin_client_grant import rich
    lines = ['🏷 <b>Скидки на подписки</b>']
    rows = []
    for scope in ('vpn', 'antig'):
        value = await active(repo, scope)
        if value:
            percent, end = value
            lines.append(f'<b>{SCOPES[scope]}</b> · {percent}% до {end.astimezone(MOSCOW):%d.%m.%Y %H:%M} МСК')
            rows.append([(f'Отключить · {SCOPES[scope]}', f'admin:discount:off:{scope}')])
        else:
            lines.append(f'<b>{SCOPES[scope]}</b> · скидки нет')
    rows.extend([[(f'➕ {label}', f'admin:input:discount:{scope}')] for scope, label in SCOPES.items()])
    rows.append([('← Продажи и тарифы', 'admin:commerce')])
    lines.append('Скидка действует на новые расчёты в Telegram и на сайте.')
    return rich('\n\n'.join(lines), rows)


async def render(repo, settings, actor, action):
    from app.services.admin_client_grant import rich
    require(settings, actor)
    parts = action.split(':')
    if parts[:3] == ['admin', 'input', 'discount'] and len(parts) == 4 and parts[3] in SCOPES:
        return rich(f'🏷 <b>{SCOPES[parts[3]]}</b>\n\n'
                    'Укажите процент и срок через запятую. Например, <code>20, 3 дня</code> '
                    'или <code>15%, 12 часов</code>. Скидка включится сразу.',
                    [[('← Скидки', 'admin:discount')]])
    if len(parts) == 4 and parts[:3] == ['admin', 'discount', 'off']:
        await save(repo, actor, parts[3])
    elif action != 'admin:discount':
        raise ValueError('Настройка недоступна.')
    return await menu(repo)


async def configure(repo, settings, actor, scope, value):
    require(settings, actor)
    parsed = re.fullmatch(r'\s*(\d{1,2})\s*%?\s*,\s*(\d{1,4})\s*(ч(?:ас(?:а|ов)?)?|д(?:ень|ня|ней|н\.?)?)\s*',
                          value.casefold())
    if not parsed:
        raise ValueError('Введите процент и срок, например «20, 3 дня» или «15, 12 часов».')
    percent, duration = map(int, parsed.group(1, 2))
    hours = duration if parsed[3].startswith('ч') else duration * 24
    if not (1 <= percent <= 99 and 1 <= hours <= 24 * 365):
        raise ValueError('Укажите скидку от 1 до 99% и срок от 1 часа до 365 дней.')
    await save(repo, actor, scope, percent, now() + timedelta(hours=hours))
    return await menu(repo)
