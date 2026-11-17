from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton, WebAppInfo


def keyboard(rows, options=None):
    options = options or {}
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, **options.get(target, {}), **({'web_app': WebAppInfo(url=target[7:])} if target.startswith('webapp:') else {'url': target[4:]} if target.startswith('url:') else {'copy_text': CopyTextButton(text=target[5:])} if target.startswith('copy:') else {'callback_data': target}))
        for label, target in row] for row in rows])


def back(target='home'):
    return [('← Назад', target)]
