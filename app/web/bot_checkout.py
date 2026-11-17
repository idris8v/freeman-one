"""Scoped, expiring payment handoff: no account login or charge on a bot view."""
import json,re
from html import escape
from fastapi import APIRouter
from fastapi.responses import HTMLResponse,RedirectResponse
from app.database.models import User,now
from app.database.repositories import SQLRepository
from app.services.bot_checkout import BotCheckout,link_key
from app.services.payment_settings import PaymentSettings

HEADERS={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Robots-Tag':'noindex, nofollow'}

def message(text,settings,status):
    link='https://t.me/'+settings.bot_username
    html='<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="icon" type="image/png" sizes="64x64" href="/assets/favicon-rounded-20261029.png"><title>Freeman ONE — оплата</title><body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;padding:32px;max-width:520px;margin:10vh auto"><h1>Оплата подписки</h1><p>'+escape(text)+'</p><a href="'+escape(link,quote=True)+'">Вернуться в Telegram</a></body></html>'
    return HTMLResponse(html,status_code=status,headers=HEADERS)

async def open_payment(database,settings,token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):raise ValueError('Ссылка недействительна. Откройте оплату заново в боте.')
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        raw=await repo.setting(link_key(token))
        info=json.loads(raw) if raw else {}
        if info.get('expires',0)<now().timestamp():raise ValueError('Ссылка устарела. Откройте оплату заново в боте.')
        await PaymentSettings(repo,settings).hydrate()
        if not settings.yookassa_shop_id or await repo.setting('payment_provider:yookassa','1')!='1':raise ValueError('Оплата сейчас недоступна.')
        user=await session.get(User,info['user_id'])
        if not user:raise ValueError('Заказ недоступен.')
        checkout=BotCheckout(repo,settings)
        payment=await checkout.owned(info['payment_id'],user)
        if payment.status=='order_paid':return None
        if payment.status=='order_canceled':raise ValueError('Счёт закрыт. Вернитесь в бот и выберите способ оплаты заново.')
        if payment.status=='order_processing':raise ValueError('Оплата обрабатывается. Дождитесь подтверждения в боте.')
        # Commit the intent before the HTTP call. A provider timeout must not
        # make the same order eligible for a second wallet payment.
        await repo.set_setting(f'bot_checkout_started:{payment.id}','1')
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        user=await session.get(User,info['user_id'])
        if not user:raise ValueError('Заказ недоступен.')
        checkout=BotCheckout(repo,settings)
        payment=await checkout.owned(info['payment_id'],user)
        if payment.status=='order_paid':return None
        if payment.status=='order_canceled':raise ValueError('Счёт закрыт. Вернитесь в бот.')
        screen=await checkout.service.render(f'order_pay:{payment.id}',user)
        url=next((t[4:] for row in screen.rows for _,t in row if t.startswith('url:https://')),None)
        if not url:raise ValueError('Не удалось открыть платёж. Проверьте его статус в боте.')
        return url

def router(database,settings):
    result=APIRouter()
    @result.get('/pay/bot/{token}')
    async def pay(token:str):
        try:url=await open_payment(database,settings,token)
        except ValueError as error:return message(str(error),settings,400)
        if url is None:return message('Оплата уже подтверждена. Ключ доступен в Telegram-боте.',settings,200)
        return RedirectResponse(url,status_code=303,headers=HEADERS)
    return result
