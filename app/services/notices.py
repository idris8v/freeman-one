"""Durable support and history delivery to explicit Telegram destinations."""
import asyncio
import json
from datetime import timedelta
from sqlalchemy import select, update
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter, TelegramNetworkError
from app.database.models import ChatNotice, WebSupportAttachment, Ticket, TicketMessage, User, ForumTask, now
from aiogram.types import FSInputFile
from pathlib import Path
from app.bot.keyboards import keyboard


SUPPORT_STORAGE=Path('/app/data/web-support')

class NoticeWorker:
    def __init__(self, database, bot, settings=None):
        self.database, self.bot = database, bot
        self.settings = settings
        self.next_chat = {}
        self.pause_until = 0
        self.last_expiry_check = -60
        self.last_support_check = -10
        from app.services.forum import ForumService
        self.forum = ForumService(database,bot)

    async def expire_idle(self):
        cutoff=now()-timedelta(hours=24)
        async with self.database.sessions.begin() as session:
            # Only unanswered requests for client input expire; support owes a reply otherwise.
            items=list(await session.scalars(select(Ticket).where(Ticket.status.in_(('waiting_client','needs_info')),Ticket.updated_at<=cutoff).with_for_update(skip_locked=True).limit(100)))
            for item in items:
                item.status='closed';item.updated_at=now()
                text='Вы не ответили в течение 24 часов после сообщения поддержки. Обращение закрыто. Если вопрос ещё актуален, откройте его снова или создайте новое обращение.'
                event=TicketMessage(ticket_id=item.id,role='system',text=text,source=f'idle-close:{item.id}:{int(item.updated_at.timestamp()*1000000)}')
                session.add(event);await session.flush()
                task=await session.scalar(select(ForumTask).where(ForumTask.ticket_id==item.id).with_for_update())
                if task:task.status='pending'
                else:session.add(ForumTask(ticket_id=item.id))
                user=await session.get(User,item.user_id)
                if user and user.telegram_id and user.telegram_id > 0:session.add(ChatNotice(dedup=f'ticket.idle:{event.id}',target_id=user.telegram_id,kind='client',ticket_id=item.id,text=text,buttons='[]'))
                from app.database.repositories import SQLRepository
                from app.services.tickets import TicketService
                await TicketService(SQLRepository(session),None).finish_dialog(item,event.id)
            return len(items)

    async def once(self):
        time = asyncio.get_running_loop().time()
        if time-self.last_expiry_check>=60:
            await self.expire_idle();self.last_expiry_check=time
            if self.settings:
                from app.services.bot_antig_addon import expire_addons
                await expire_addons(self.database,self.settings)
        if self.settings and time-self.last_support_check>=10:
            from app.services.support_alerts import queue
            await queue(self.database,self.settings)
            self.last_support_check=time
        if time < self.pause_until:
            return False
        async with self.database.sessions.begin() as session:
            await session.execute(update(ChatNotice).where(ChatNotice.status=='sending',ChatNotice.kind!='support_media_reply',ChatNotice.available_at<=now()).values(status='pending'))
            candidates = list(await session.scalars(select(ChatNotice).where(ChatNotice.status=='pending',ChatNotice.available_at<=now()).order_by(ChatNotice.id).limit(100)))
            job = next((job for job in candidates if self.next_chat.get(job.target_id,0)<=time),None)
            if job is None:
                return False
            changed = await session.execute(update(ChatNotice).where(ChatNotice.id==job.id,ChatNotice.status=='pending').values(status='sending',attempts=ChatNotice.attempts+1,available_at=now()+timedelta(minutes=5)))
            if not changed.rowcount:
                return False
            id,target,text,photo,document,buttons,attempts,kind,ticket_id = job.id,job.target_id,job.text,job.photo,job.document,job.buttons,job.attempts,job.kind,job.ticket_id
            if ticket_id and kind in ('support','support_home'):
                job.status='cancelled'
                return True
            if kind=='support_home':
                from app.database.repositories import SQLRepository
                ticket=await session.get(Ticket,ticket_id)
                selected=await SQLRepository(session).setting(f'support_dialog:{ticket.user_id}','0') if ticket else '0'
                if not ticket or ticket.status!='closed' or selected=='1':
                    job.status='cancelled'
                    return True
            if ticket_id and kind=='client' and job.dedup.startswith('ticket.asset:'):
                job.status='cancelled'
                return True
            rich_screen=None
            direct_media=None
            if kind=='support_media_reply':
                from app.web.support_telegram_media import prepare
                try:direct_media=await prepare(session,job,SUPPORT_STORAGE)
                except ValueError:
                    job.status='failed';return True
                if direct_media is None:
                    job.status='cancelled';return True
            if kind=='admin_client_change':
                from app.config.settings import Settings
                from app.database.repositories import SQLRepository
                from app.services.admin_client_notices import screen
                rich_screen=await screen(SQLRepository(session),self.settings or Settings(),job)
                if rich_screen is None:
                    job.status='cancelled'
                    return True
            if kind=='client_grant':
                from app.database.repositories import SQLRepository
                from app.services.admin_client_grant import delivery_screen
                rich_screen=await delivery_screen(SQLRepository(session),job)
                if rich_screen is None:
                    job.status='cancelled'
                    return True
            if ticket_id and kind=='client':
                from app.config.settings import Settings
                from app.database.repositories import SQLRepository
                from app.services.support_alerts import client_screen
                rich_screen=await client_screen(SQLRepository(session),self.settings or Settings(),job)
                if rich_screen is None:
                    job.status='cancelled';return True
                photo=document=None
            if kind in ('trial_offer','trial_convert'):
                from app.config.settings import Settings
                from app.database.repositories import SQLRepository
                from app.services.trial_offer import conversion_screen, notice_screen
                trial_screen=conversion_screen if kind=='trial_convert' else notice_screen
                rich_screen=await trial_screen(SQLRepository(session),self.settings or Settings(),job)
                if rich_screen is None:
                    job.status='cancelled'
                    return True
            if kind=='partner_debt':
                from app.database.repositories import SQLRepository
                from app.services.partner_ledger import reminder_screen
                user=await session.scalar(select(User).where(User.telegram_id==target))
                rich_screen=await reminder_screen(SQLRepository(session),user)
                if rich_screen is None:
                    job.status='cancelled'
                    return True
            if kind == 'public_proxy_offer':
                from app.database.repositories import SQLRepository
                from app.services.public_proxy import purchase_notice_screen
                rich_screen = await purchase_notice_screen(SQLRepository(session), job)
                if rich_screen is None:
                    job.status = 'cancelled'
                    return True
            if kind=='proxy_gift':
                from app.config.settings import Settings
                from app.database.repositories import SQLRepository
                from app.services.proxy_control import current_proxy_gift
                from app.services.public_proxy import connection_url
                repo = SQLRepository(session)
                rich_screen = None if await connection_url(repo) else await current_proxy_gift(repo,self.settings or Settings(),job)
                if rich_screen is None:
                    job.status='cancelled'
                    return True
            if kind == 'support_group':
                from app.database.repositories import SQLRepository
                from app.services.support_group import screen_for
                if not self.settings:
                    job.status='cancelled';return True
                rich_screen = await screen_for(SQLRepository(session), self.settings, job)
                if rich_screen is None:
                    job.status='cancelled';return True
            if kind=='support_alert':
                from app.database.repositories import SQLRepository
                from app.services.support_alerts import screen as support_alert_screen
                if not self.settings:
                    job.status='cancelled';return True
                rich_screen=await support_alert_screen(SQLRepository(session),self.settings,job)
                if rich_screen is None:
                    job.status='cancelled';return True
            if kind in ('history','history_digest','infra_alert'):
                from app.services.admin_notifications import event_screen,digest_screen
                from app.database.repositories import SQLRepository
                repo=SQLRepository(session)
                if kind=='history_digest':
                    if await repo.setting('history_chat_id') != str(target) or await repo.setting('history_daily_enabled','1') != '1':
                        job.status='cancelled';return True
                    try:rich_screen=await digest_screen(repo,job.dedup.split(':',1)[1])
                    except (ValueError,KeyError,TypeError):job.status='failed';return True
            if kind in ('history', 'infra_alert'):
                from app.database.models import Activity
                from app.database.repositories import SQLRepository
                from app.services.activity import infrastructure_target, publish_to_history
                event = await session.get(Activity,int(job.dedup.split(':')[1])) if job.dedup.startswith('activity:') else None
                if event and event.dedup.startswith('infra.alert:'):
                    destination = await infrastructure_target(repo)
                    if destination and destination != str(target):
                        job.target_id = int(destination)
                        job.kind = 'infra_alert'
                        job.status = 'pending'
                        job.available_at = now()
                        job.attempts = max(0, job.attempts - 1)
                        return True
                else:
                    destination = await repo.setting('history_chat_id')
                if destination != str(target) or not event or not await publish_to_history(repo,event):
                    await session.execute(update(ChatNotice).where(ChatNotice.id==id).values(status='cancelled'))
                    return True
                if event:rich_screen=await event_screen(SQLRepository(session),event,self.settings)
        status,delay,message_id,media_id = 'sent',0,None,None
        self.next_chat[target]=time+(3.2 if target<0 else 1.1)
        try:
            thread_id = await self.forum.ensure(target,ticket_id) if kind=='support' and target<0 and ticket_id else None
            routing = {'message_thread_id':thread_id} if thread_id else {}
            if thread_id:
                buttons='[]'
            markup=keyboard(json.loads(buttons)) if buttons!='[]' else None
            if direct_media is not None:
                result=await self.bot.send_rich_message(target,direct_media,**routing)
                message_id=result.message_id
            elif rich_screen is not None:
                markup=keyboard(rich_screen.rows,rich_screen.button_options) if rich_screen.rows else markup
                from app.bot.handlers.navigation import navigation_rich
                try:result=await self.bot.send_rich_message(target,navigation_rich(rich_screen),reply_markup=markup,**routing)
                except TelegramBadRequest:
                    if kind in ('proxy_gift', 'public_proxy_offer'):
                        try:
                            result=await self.bot.send_photo(target,rich_screen.photo,caption=rich_screen.text,parse_mode='HTML',reply_markup=markup,**routing)
                        except TelegramBadRequest:
                            result=await self.bot.send_message(target,rich_screen.text,parse_mode='HTML',reply_markup=markup,**routing)
                    else:
                        result=await self.bot.send_message(target,rich_screen.text,parse_mode='HTML',reply_markup=markup,**routing)
                message_id=result.message_id
            elif kind=='support_home':
                await self.send_home(target,ticket_id)
            elif photo or document:
                send = self.bot.send_photo if photo else self.bot.send_document
                media = photo or document
                if media.startswith('web-support:'):
                    async with self.database.sessions() as session:
                        asset=await session.get(WebSupportAttachment,int(media.split(':')[1]))
                        if not asset:raise ValueError('Вложение отсутствует')
                        path=SUPPORT_STORAGE/asset.storage_name
                        if not path.is_file():raise ValueError('Файл отсутствует')
                        media=FSInputFile(path,filename=asset.original_name)
                        if asset.kind=='video':send=self.bot.send_video
                if len(text)<=1024:
                    result = await send(target,media,caption=text,parse_mode=None,reply_markup=markup,**routing)
                    message_id = result.message_id
                else:
                    result = await send(target,media,caption='📎 Вложение к обращению',parse_mode=None,**routing)
                    media_id=result.message_id
                    # Two messages to one chat are separated to respect rate limits.
                    await asyncio.sleep(3.2 if target<0 else 1.1)
                    result = await self.bot.send_message(target,text,parse_mode=None,reply_markup=markup,**routing)
                    message_id=result.message_id
                    self.next_chat[target]=asyncio.get_running_loop().time()+(3.2 if target<0 else 1.1)
            else:
                result = await self.bot.send_message(target,text,parse_mode=None,reply_markup=markup,**routing)
                message_id=result.message_id
        except TelegramRetryAfter as error:
            status,delay='pending',error.retry_after+1
            self.pause_until=asyncio.get_running_loop().time()+delay
        except (TelegramForbiddenError,TelegramBadRequest):
            status='failed'
        except ValueError:
            status='failed'
        except (TelegramNetworkError,TimeoutError):
            status,delay=('uncertain',0) if kind=='support_media_reply' else (('failed',0) if attempts>=5 else ('pending',min(3600,2**attempts*10)))
        self.next_chat[target]=asyncio.get_running_loop().time()+(3.2 if target<0 else 1.1)
        async with self.database.sessions.begin() as session:
            await session.execute(update(ChatNotice).where(ChatNotice.id==id,ChatNotice.status=='sending').values(status=status,
                available_at=now()+timedelta(seconds=delay),message_id=message_id,media_message_id=media_id))
        return True

    async def send_home(self, target, ticket_id):
        # Reuse the same decorated home renderer as /start, including configured media.
        from app.config.settings import Settings
        from app.database.repositories import SQLRepository
        from app.services.screens import ScreenService
        from app.bot.handlers.navigation import show
        from aiogram.types import Message, Chat
        async with self.database.sessions.begin() as session:
            ticket=await session.get(Ticket,ticket_id)
            user=await session.get(User,ticket.user_id)
            if not user or user.telegram_id != target or target <= 0:
                raise ValueError('Telegram account changed')
            screens=ScreenService(SQLRepository(session),Settings(),None,None)
            screens.preserve_credentials=True
            screen=await screens.render('home',user)
        event=Message(message_id=0,date=now(),chat=Chat(id=target,type='private')).as_(self.bot)
        await show(event,screen)
