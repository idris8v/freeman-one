"""One private support forum topic per customer, persistent across tickets."""
import asyncio
from sqlalchemy import select, update
from aiogram.exceptions import TelegramBadRequest
from app.database.models import ForumThread, Ticket, User
from app.database.repositories import SQLRepository
from app.bot.keyboards import keyboard


class ForumService:
    def __init__(self, database, bot):
        self.database, self.bot = database, bot

    async def ensure(self, chat_id, ticket_id):
        async with self.database.sessions() as session:
            ticket = await session.get(Ticket,ticket_id)
            user = await session.get(User,ticket.user_id)
            link = await session.scalar(select(ForumThread).where(ForumThread.chat_id==chat_id,ForumThread.user_id==user.id))
            thread_id = link.thread_id if link else None
        if thread_id is None:
            chat = await self.bot.get_chat(chat_id)
            if not chat.is_forum:
                raise ValueError('Support group must enable topics')
            topic = await self.bot.create_forum_topic(chat_id, name=f'{user.display_name[:75]} · {user.telegram_id}')
            thread_id = topic.message_thread_id
            async with self.database.sessions.begin() as session:
                link = ForumThread(chat_id=chat_id,user_id=user.id,thread_id=thread_id)
                await SQLRepository(session).add(link)
        await self.reopen(chat_id,thread_id)
        header_sent = await self.update_header(chat_id,user.id)
        if header_sent:
            await asyncio.sleep(3.2)
        return thread_id

    async def reopen(self,chat_id,thread_id):
        try:
            await self.bot.reopen_forum_topic(chat_id,thread_id)
        except TelegramBadRequest as error:
            if 'TOPIC_NOT_MODIFIED' not in error.message.upper():
                raise
        async with self.database.sessions.begin() as session:
            await session.execute(update(ForumThread).where(ForumThread.chat_id==chat_id,ForumThread.thread_id==thread_id).values(closed=False))

    async def update_header(self,chat_id,user_id):
        async with self.database.sessions() as session:
            link = await session.scalar(select(ForumThread).where(ForumThread.chat_id==chat_id,ForumThread.user_id==user_id))
            if not link:
                return False
            user = await session.get(User,user_id)
            ticket = await session.scalar(select(Ticket).where(Ticket.user_id==user_id).order_by(Ticket.id.desc()).limit(1))
            from app.services.tickets import STATUSES
            text = f'🛟 Клиент: {user.display_name}\nTelegram ID: {user.telegram_id}\n'+(f'Username: @{user.username}\n' if user.username else '')
            text += f'Тикет #{ticket.id} · {STATUSES[ticket.status]}\n\nПишите в этой теме — ответ уйдёт этому клиенту.\n/close — закрыть обращение. Новый вопрос клиента откроет эту же тему.'
            rows = [[('✅ Закрыть обращение',f'admin:t:topicclose:{user_id}')]] if ticket.status!='closed' else []
            rows += [[('📖 Как работает поддержка','admin:t:guide')]]
            if user.telegram_id: rows.append([('💬 Контакт',f'url:tg://user?id={user.telegram_id}')])
            message_id,thread_id=link.control_message_id,link.thread_id
        if message_id:
            try:
                await self.bot.edit_message_text(text,chat_id=chat_id,message_id=message_id,reply_markup=keyboard(rows),parse_mode=None)
                return False
            except TelegramBadRequest as error:
                if 'message is not modified' in error.message.lower():
                    return False
                if 'message to edit not found' not in error.message.lower():
                    raise
        result = await self.bot.send_message(chat_id,text,message_thread_id=thread_id,reply_markup=keyboard(rows),parse_mode=None)
        async with self.database.sessions.begin() as session:
            await session.execute(update(ForumThread).where(ForumThread.chat_id==chat_id,ForumThread.user_id==user_id).values(control_message_id=result.message_id))
        return True

    async def close_once(self):
        return False  # Support now lives in the website / Mini App.

    async def refresh_ticket(self,ticket_id):
        async with self.database.sessions() as session:
            ticket=await session.get(Ticket,ticket_id)
            target=await SQLRepository(session).setting('support_chat_id')
            if not ticket or not target:
                return None
            chat_id=int(target);user_id=ticket.user_id
        await self.update_header(chat_id,user_id)
        return chat_id


async def current_topic_ticket(repo,chat_id,thread_id):
    if not thread_id:
        return None
    link=await repo.session.scalar(select(ForumThread).where(ForumThread.chat_id==chat_id,ForumThread.thread_id==thread_id))
    if not link:
        return None
    return await repo.session.scalar(select(Ticket).where(Ticket.user_id==link.user_id,Ticket.status!='closed').order_by(Ticket.id.desc()).limit(1))
