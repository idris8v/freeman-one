"""Read-only Telegram issue summary; keep existing owner-sale accounting intact."""
import json
from html import escape

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.bot.rich_copy import text_blocks
from aiogram.types import InputRichMessage
from app.services.screens import Screen, duration, money


async def today_summary(repo):
    # The website's authoritative sales definition excludes top-ups and free
    # grants. Reuse it read-only, in a single aggregate rather than a full report.
    from app.web.admin_reports import SALES, period_bounds
    lo, hi = period_bounds('day', None, None)
    params = dict(lo=lo, hi=hi, kind='all', source='all', months=-1, devices=0,
                  channel='all', sale_type='all')
    async with repo.session.begin_nested():
        row = (await repo.session.execute(text(SALES+'SELECT count(*) sales, coalesce(sum(amount),0) revenue FROM selected'), params)).mappings().one()
    return int(row['revenue']), int(row['sales'])


async def issue_screen(service, user, tariff, target, sid, ready):
    repo, settings = service.repo, service.settings
    data = json.loads(await repo.setting('admin_grant:'+target.rsplit(':', 1)[1], '{}'))
    terms = data['sale_terms']
    owner_sale = ready and settings.is_owner(user.telegram_id)
    title = 'Продление подписки' if sid else 'Выдача подписки'
    text_value = (
        f'🧾 <b>{title}</b>\n\n<b>Тариф:</b> {escape(terms["tariff_name"])}'
        f'\n<b>Срок:</b> {duration(terms["months"])}\n<b>Устройства:</b> {terms["devices"]}'
    )
    if terms.get('bot_test_access'):
        text_value += ('\n\n<b>Антиглушилка:</b> '+('Да' if len(terms['bot_test_access']['squads'])==2 else 'Нет')
            +'\n<b>Доплата:</b> '+money(terms.get('antig_surcharge',0)))
    if owner_sale:
        text_value += f'\n\n<b>Цена:</b> {money(terms["total"])}'
        try:
            revenue, sales = await today_summary(repo)
            text_value += (f'\n\n📊 <b>Сегодня</b>\nПродаж: {sales}'
                f'\nВыручка: {money(revenue)}'
                f'\nПосле продажи: <b>{money(revenue+terms["total"])}</b>')
        except SQLAlchemyError:
            text_value += '\n\nСтатистика временно недоступна.'
        label = ('✅ Продлить и учесть ' if sid else '✅ Выдать и учесть ')+money(terms['total'])
    else:
        text_value += '\n\nБесплатная выдача сотруднику. Без списания и без дохода от продажи.'
        if not ready:
            text_value += '\n\nРабочий VPN-сервер пока не подключён. Эта выдача не будет учтена как продажа.'
        label = '🎁 Получить бесплатно'
    back = f'renew_devices:{sid}:{terms["devices"]}' if sid else f'buy_devices:{terms["devices"]}'
    rows=[[(label,target)]]
    if owner_sale:
        rows.append([('🎁 Продлить без учёта продажи' if sid else '🎁 Выдать бесплатно / замена',target+':free')])
    rows.append([('← Назад',back)])
    return Screen(text_value, rows,
                  rich=InputRichMessage(blocks=text_blocks(text_value), skip_entity_detection=True), structured=True, appearance_page='pay')
