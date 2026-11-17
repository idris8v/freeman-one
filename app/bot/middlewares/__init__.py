import logging
import asyncio
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, PreCheckoutQuery
from app.database.repositories import SQLRepository
from app.services.domain import ReferralService
from app.services.screens import ScreenService

logger = logging.getLogger(__name__)
ERROR_RESPONSE_SECONDS = 3


async def report_error(event, *, checkout=False):
    text = 'Что-то пошло не так. Попробуйте ещё раз.'
    if isinstance(event, CallbackQuery):
        reply = event.answer(text, show_alert=True)
    elif checkout:
        reply = event.answer(ok=False, error_message=text)
    else:
        reply = event.answer(text)
    await asyncio.wait_for(reply, timeout=ERROR_RESPONSE_SECONDS)


class ServicesMiddleware(BaseMiddleware):
    def __init__(self, database, settings, payment, provisioning):
        self.database, self.settings = database, settings
        self.payment, self.provisioning = payment, provisioning

    async def __call__(self, handler, event, data):
        from app.bot.latency import current,obsolete
        import time
        trace=current.get()
        if trace:trace.wait_ms=(time.perf_counter()-trace.started)*1000
        if obsolete():return
        if event.from_user is None:
            return
        if isinstance(event, CallbackQuery) and event.message is None:
            await event.answer()
            return
        checkout = isinstance(event, PreCheckoutQuery)
        chat = None if checkout else event.chat if isinstance(event, Message) else event.message.chat
        group = bool(chat and chat.type in ('group', 'supergroup'))
        if chat and chat.type != 'private' and not group:
            return
        if isinstance(event, CallbackQuery) and (event.data or '').startswith('sales_report:'):
            from app.services.bot_rich_report import handle
            try:
                async with self.database.sessions() as session:
                    await handle(event, SQLRepository(session), self.settings)
            except Exception:
                logger.exception('Sales report callback failed')
                await report_error(event)
            return
        if isinstance(event, CallbackQuery) and (event.data or '').startswith('support_group:resume:'):
            from app.services.support_group import resume
            if not group or not self.settings.can_admin_action(event.from_user.id, 'admin:t:home'):
                await event.answer('Недостаточно прав.', show_alert=True)
                return
            try:
                async with self.database.sessions.begin() as session:
                    repo = SQLRepository(session)
                    actor = await repo.user(event.from_user.id)
                    if not actor or actor.status != 'active':
                        raise PermissionError('Недостаточно прав.')
                    await resume(repo, self.settings, actor, chat.id, event.data)
            except (PermissionError, ValueError) as error:
                await event.answer(str(error), show_alert=True)
            else:
                await event.answer('Диалог передан агенту.', show_alert=True)
            return
        if group and not self.settings.can_admin_action(event.from_user.id, 'admin:t:home'):
            if isinstance(event, CallbackQuery):
                await event.answer('Этот раздел доступен только владельцу бота.', show_alert=True)
            return
        if isinstance(event,CallbackQuery) and not data.get('callback_ack_started'):
            from app.bot.handlers.navigation import start_callback_ack
            start_callback_ack(event);data['callback_ack_started']=True
        try:
            async with self.database.sessions() as session:
                # Persist registration separately. A temporary Telegram API failure while
                # drawing the first screen must never roll back a newly created customer.
                async with session.begin():
                    repo = SQLRepository(session)
                    if group:
                        support_chat = await repo.setting('support_chat_id')
                        command = (event.text or '').split('@')[0].split()[0] if isinstance(event, Message) and event.text else ''
                        binding = command in ('/bind_support', '/bind_suppor', '/bind_history', '/bind_infra', '/bind_support_alerts')
                        if not binding and (support_chat != str(chat.id) or (isinstance(event, CallbackQuery) and not (event.data or '').startswith(('admin:t:', 'admin:guide:')))):
                            if isinstance(event, CallbackQuery):
                                await event.answer('Чат не привязан к поддержке.', show_alert=True)
                            return
                    data['group_context'] = group
                    payload = ''
                    if isinstance(event, Message) and event.text:
                        command, _, arguments = event.text.partition(' ')
                        if command.split('@')[0] == '/start':
                            payload = arguments.strip()
                    if payload.startswith('link_') and not group:
                        from app.services.accounts import link_telegram
                        try:
                            data['user']=await link_telegram(repo,payload[5:],event.from_user)
                        except ValueError as error:
                            await event.answer(str(error))
                            return
                    else:
                        registration_payload=payload
                        if payload.startswith('auth_') and not group:
                            from app.services.telegram_auth import challenge
                            approval=await challenge(session,payload[5:])
                            if approval and approval.email and approval.email.isdigit():
                                registration_payload='ref_account_'+approval.email
                        data['user'] = await ReferralService(repo).register(event.from_user, registration_payload)
                    track_start = isinstance(event,Message) and event.text and event.text.partition(' ')[0].split('@')[0]=='/start'
                async with session.begin():
                    repo = SQLRepository(session)
                    # Refresh persisted staff permissions on every private update:
                    # revocation also invalidates old issue/device buttons.
                    if not group:
                        role=await repo.setting(f'staff_role:{event.from_user.id}')
                        blocked=await repo.setting(f'staff_blocked:{event.from_user.id}')=='1'
                        self.settings.set_staff_role(event.from_user.id,None if blocked else role)
                    if data['user'].status == 'blocked' and not self.settings.is_admin(event.from_user.id):
                        if isinstance(event, CallbackQuery):
                            await event.answer('Доступ ограничен. Обратитесь в поддержку.', show_alert=True)
                        elif checkout:
                            await event.answer(ok=False, error_message='Доступ ограничен. Обратитесь в поддержку.')
                        else:
                            await event.answer('Доступ ограничен. Обратитесь в поддержку.')
                        return
                    data['screens'] = ScreenService(repo, self.settings, self.payment, self.provisioning)
                    result = await handler(event, data)
                    # Record after the response so a slow Telegram API request does not
                    # keep SQLite's write lock while other customers open the bot.
                    if track_start:
                        if not group:
                            from app.services.trial_offer import queue
                            await queue(repo,self.settings,data['user'])
                        await repo.track_funnel(data['user'].id,'start',payload)
                    return result
        except Exception:
            logger.exception('Update processing failed')
            try:
                await report_error(event, checkout=checkout)
            except Exception:
                logger.exception('Could not deliver error notification')
