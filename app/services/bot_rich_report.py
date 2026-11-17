"""In-message navigation for a saved sales report."""
import json
from datetime import date

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputRichBlockButtons, InputRichMessage, RichMessageButton

from app.services.bot_catalog import rich_table

PREFIX = 'sales_report:'
PAGES = {
    'day': 'К сводке',
    'vpn': 'Продажи VPN за день',
    'antig': 'Выручка от антиглушилки',
}


def storage_key(message_id):
    return f'bot_rich_report:{message_id}'


def button(target, title, *, style='primary'):
    return RichMessageButton(text=title, callback_data=PREFIX + target, style=style)


def render(report, page):
    periods = report.get('antig_periods', {})
    if page == 'antig_dates':
        dates = list(periods)
        blocks = [{'type': 'heading', 'text': 'С какого дня считать?', 'size': 2}]
        for offset in range(0, len(dates), 4):
            blocks.append(InputRichBlockButtons(buttons=[
                button('antig:' + day, periods[day]['label']) for day in dates[offset:offset + 4]
            ]))
        blocks.append(InputRichBlockButtons(buttons=[button('antig', 'За весь период', style='link')]))
        return InputRichMessage(blocks=blocks, skip_entity_detection=True)
    data = periods[page.split(':', 1)[1]] if page.startswith('antig:') else report[page]
    table = rich_table(data['title'], data['intro'], data['headers'], data['rows'])
    if page == 'day':
        buttons = [button('vpn', PAGES['vpn']), button('antig', PAGES['antig'])]
    else:
        buttons = []
        if page.startswith('antig') and periods:
            buttons.append(button('antig_dates', 'Выбрать дату начала'))
        buttons.append(button('day', PAGES['day'], style='link'))
    return InputRichMessage(
        blocks=[*table.blocks, *(InputRichBlockButtons(buttons=[item]) for item in buttons)],
        skip_entity_detection=True,
    )


async def handle(query, repo, settings):
    page = (query.data or '').removeprefix(PREFIX)
    message = query.message
    if (not settings.can_admin_action(query.from_user.id, 'admin:sales')
            or message.chat.type not in ('group', 'supergroup')
            or str(message.chat.id) != await repo.setting('history_chat_id')):
        await query.answer('Отчёт доступен администраторам в чате продаж.', show_alert=True)
        return
    document = await repo.setting(storage_key(message.message_id))
    if not document:
        await query.answer('Этот отчёт недоступен.', show_alert=True)
        return
    report = json.loads(document)
    if report.get('chat') != str(message.chat.id):
        await query.answer('Этот отчёт недоступен.', show_alert=True)
        return
    try:
        if report.get('calendar_start'):
            from app.services.bot_daily_report import render_calendar
            rich = await render_calendar(repo, page, date.fromisoformat(report['calendar_start']), button)
        else:
            periods = report.get('antig_periods', {})
            permitted = (page in PAGES or (page == 'antig_dates' and periods)
                         or (page.startswith('antig:') and page.split(':', 1)[1] in periods))
            if not permitted:
                raise ValueError('Unknown report')
            rich = render(report, page)
        await query.bot.edit_message_text(chat_id=message.chat.id, message_id=message.message_id,
                                          rich_message=rich)
    except ValueError:
        await query.answer('Отчёт за эту дату недоступен.', show_alert=True)
        return
    except TelegramBadRequest as error:
        if 'message is not modified' not in str(error).casefold():
            await query.answer('Не удалось открыть отчёт.', show_alert=True)
            return
    await query.answer()
