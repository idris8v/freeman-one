"""Refresh the active administrator support menu without sending new messages."""
import asyncio
import logging
from sqlalchemy import select,func
from aiogram.exceptions import TelegramAPIError,TelegramBadRequest
from app.database.models import Ticket,User
from app.bot.keyboards import keyboard
from app.services.web_login import issue_direct
from urllib.parse import urlencode

async def attention_count(session):
    # The Mini App presents one conversation per customer. Count the latest
    # ticket only, excluding conversations already waiting for the customer.
    latest=select(Ticket.status.label('status'),func.row_number().over(
        partition_by=Ticket.user_id,order_by=(Ticket.updated_at.desc(),Ticket.id.desc())).label('rank')).subquery()
    return int(await session.scalar(select(func.count()).select_from(latest).where(
        latest.c.rank==1,latest.c.status.in_(('waiting_admin','in_progress')))) or 0)

menus={}
def track(chat_id,message_id,screen,telegram_id):
    key=(chat_id,message_id)
    if any('mode=staff' in target for row in screen.rows for _,target in row):
        menus[key]=(screen.rows,screen.button_options,telegram_id)
    else:menus.pop(key,None)

async def refresh(database,bot,settings):
    if not menus:return
    async with database.sessions() as session:
        count=await attention_count(session)
        for key,(rows,options,telegram_id) in list(menus.items()):
            if not settings.can_admin_action(telegram_id,'admin:t:home'):menus.pop(key,None);continue
            user=await session.scalar(select(User).where(User.telegram_id==telegram_id))
            if not user or user.status!='active':menus.pop(key,None);continue
            target='webapp:'+settings.web_base_url+'/auth/bot/support?'+urlencode({'mode':'staff','token':issue_direct(settings,user,purpose='direct_support:staff')})
            updated=[[(f'🗂 Обращения · {count}',target) if 'mode=staff' in old else (label,old) for label,old in row] for row in rows]
            try:
                await bot.edit_message_reply_markup(chat_id=key[0],message_id=key[1],reply_markup=keyboard(updated,options))
            except TelegramBadRequest:menus.pop(key,None)
            except TelegramAPIError:continue

async def run(database,bot,settings):
    while True:
        await asyncio.sleep(60)
        try:await refresh(database,bot,settings)
        except Exception:logging.getLogger(__name__).exception('Support menu counter update failed')

