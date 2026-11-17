"""Admin-only opt-in pilot. No public prices, customer checkout or schema edits."""
import json
from html import escape
from uuid import UUID

from sqlalchemy import select

from app.database.models import RemnawaveServer
from app.services.screens import Screen, duration, money


def screen(text, rows):
    from aiogram.types import InputRichMessage
    from app.bot.rich_copy import text_blocks
    return Screen(text, rows, rich=InputRichMessage(blocks=text_blocks(text), skip_entity_detection=True), structured=True)


def choice_screen(plan, surcharge, yes, no, back, *, base=None, partner=False):
    explanation = ('📡 <b>Что это?</b>\nДополнительный сервер для подключения при ограничениях мобильного интернета '
        '(например, во время угрозы БПЛА).\n\n'
        '📱 <b>Как пользоваться?</b>\nОн появится в вашем VPN-приложении вместе с обычными серверами. '
        'При необходимости выберите его в списке. Второй ключ устанавливать не нужно.\n\n'
        '⚠️ Работа зависит от оператора и ограничений. При полном отключении мобильной связи подключение не гарантируется.')
    text = ('🛡️ <b>Нужна антиглушилка?</b>\n\n📦 '+escape(plan)+'\n\n'+explanation
            +'\n\n💰 <b>Доплата за выбранный срок: '+money(surcharge)+'</b>')
    if partner:
        text += ('\n\n💼 В долг за обычный ключ: '+money(base)
                 +'\nВ долг с антиглушилкой: <b>'+money(base+surcharge)+'</b>'
                 +'\nНачислим после выдачи ключа.')
    elif base is not None:
        text += '\nОбычная подписка: '+money(base)+'\nС антиглушилкой: <b>'+money(base+surcharge)+'</b>'
    else:
        text += '\nДобавляется к цене подписки только при выборе «Да».'
    result = screen(text, [[('✅ Да, добавить антиглушилку · +'+money(surcharge),yes)],
                           [('➡️ Нет, оставить обычную подписку',no)],
                           [('← Изменить срок',back)]])
    result.button_options[yes] = {'style':'success'}
    result.button_options[no] = {'style':'primary'}
    return result


def validate_profile(value):
    if not isinstance(value, dict) or type(value.get('panel')) is not int or value['panel'] < 1:
        raise ValueError('Некорректная настройка выдачи.')
    ids = value.get('squads')
    if not isinstance(ids, list) or len(ids) not in (1, 2) or len(set(ids)) != len(ids):
        raise ValueError('Не подтверждены сквады выдачи.')
    for identity in ids:
        if not isinstance(identity, str) or str(UUID(identity)) != identity:
            raise ValueError('Не подтверждены сквады выдачи.')
    return value


async def profile(repo, sid):
    raw = await repo.setting(f'bot_test_access:{sid}', '')
    return validate_profile(json.loads(raw)) if raw else None


async def creation_squads(repo, sub, panel):
    value = await profile(repo, sub.id)
    if not value:
        return [panel.internal_squad_uuid]
    if panel.id != value['panel'] or panel.internal_squad_uuid != value['squads'][0]:
        raise ValueError('Настройки панели изменились. Выдача остановлена.')
    return value['squads']


async def resolve(repo, settings, enabled):
    """Read-only discovery; ambiguous or empty squads fail closed."""
    from app.services.remnawave import RemnawaveService
    panels = list(await repo.session.scalars(select(RemnawaveServer).where(RemnawaveServer.status == 'active')))
    matches = []
    for panel in panels:
        if panel.health == 'offline' or not panel.internal_squad_uuid:
            continue
        result = await RemnawaveService(repo, settings).api(panel).squads()
        data = result.get('response', result)
        items = data.get('internalSquads', data.get('items', [])) if isinstance(data, dict) else data
        standard = [x for x in items if x.get('uuid') == panel.internal_squad_uuid]
        anti = [x for x in items if str(x.get('name', '')).casefold().replace('-', '').replace(' ', '') == 'antig']
        if len(standard) != 1 or len(anti) != 1 or standard[0]['uuid'] == anti[0]['uuid']:
            continue
        if not standard[0].get('inbounds') or not anti[0].get('inbounds'):
            continue
        matches.append(dict(panel=panel.id, squads=[standard[0]['uuid']] + ([anti[0]['uuid']] if enabled else [])))
    if len(matches) != 1:
        raise ValueError('Не удалось однозначно выбрать обычный сквад и сквад антиглушилки. Ключ не выдан.')
    return validate_profile(matches[0])


async def render(service, action, user):
    repo, settings = service.repo, service.settings
    if not settings.is_owner(user.telegram_id):
        raise PermissionError('Раздел доступен только администратору.')
    from app.services.bot_catalog import available, devices_label
    tariffs = await available(repo)
    p = action.split(':')
    from app.services.bot_antig_price import require_enabled
    if not (len(p)>=4 and p[1]=='tariff' and p[3]=='no'):
        await require_enabled(repo)
    if len(p) == 1:
        rows = [[('📱 ' + devices_label(n), f'antig_test:devices:{n}')] for n in sorted({t.device_limit for t in tariffs})]
        return screen('🛡️ <b>Антиглушилка</b>\n\nВыберите количество устройств.\n\nУсловия выдачи и учёт выберете перед подтверждением.', rows + [[('← Назад', 'buy')]])
    if p[1] == 'devices' and len(p) == 3:
        devices = int(p[2])
        chosen = sorted((t for t in tariffs if t.device_limit == devices), key=lambda t: (t.months, t.id))
        if not chosen:
            raise ValueError('Тарифы недоступны.')
        rows = [[(duration(t.months) + ' · ' + money(t.price), f'antig_test:tariff:{t.id}')] for t in chosen]
        return screen('📅 <b>На какой срок?</b>\n\n' + devices_label(devices) + '\n\nВыберите срок подписки. Итоговая цена появится перед подтверждением.', rows + [[('← Назад', 'antig_test')]])
    if p[1] != 'tariff' or len(p) not in (3, 4, 5):
        raise ValueError('Выбор недоступен.')
    tariff = next((t for t in tariffs if t.id == int(p[2])), None)
    if not tariff:
        raise ValueError('Тариф недоступен.')
    from app.services.bot_antig_price import tariff_total
    surcharge = await tariff_total(repo, tariff)
    base = f'{devices_label(tariff.device_limit)} · {duration(tariff.months)}'
    if len(p) == 3:
        yes, no = action+':yes:'+str(surcharge), action+':no:'+str(surcharge)
        return choice_screen(base,surcharge,yes,no,f'buy_devices:{tariff.device_limit}')
    if p[3] not in ('yes', 'no'):
        raise ValueError('Выберите «Да» или «Нет».')
    if len(p)==5 and p[4]!=str(surcharge):
        updated=await render(service,':'.join(p[:3]),user)
        updated.text='🔄 Доплата изменилась. Проверьте новую цену и выберите ещё раз.\n\n'+updated.text
        from aiogram.types import InputRichMessage
        from app.bot.rich_copy import text_blocks
        updated.rich=InputRichMessage(blocks=text_blocks(updated.text),skip_entity_detection=True)
        return updated
    enabled = p[3] == 'yes'
    access = await resolve(repo, settings, enabled)
    from app.services.admin_subscription import preview
    target = await preview(repo, user, f'id{tariff.id}')
    token = target.rsplit(':', 1)[1]
    data = json.loads(await repo.setting('admin_grant:' + token))
    data['antig_test'] = True
    data['sale_terms']['bot_test_access'] = access
    data['sale_terms']['antig_base_total'] = data['sale_terms']['total']
    data['sale_terms']['antig_surcharge'] = surcharge if enabled else 0
    if data['sale_terms']['total'] + (surcharge if enabled else 0) > 100000000:
        raise ValueError('Итоговая цена слишком велика. Проверьте настройки.')
    data['sale_terms']['total'] += surcharge if enabled else 0
    data['sale_terms']['base_price'] += surcharge if enabled else 0
    await repo.set_setting('admin_grant:' + token, json.dumps(data))
    from app.services.bot_admin_checkout import issue_screen
    result = await issue_screen(service, user, tariff, target, 0, await service.vpn_ready())
    result.rows[-1] = [('← Назад', ':'.join(p[:3]))]
    return result
