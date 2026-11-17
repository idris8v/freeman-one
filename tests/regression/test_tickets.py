import asyncio
from types import SimpleNamespace
from datetime import timedelta
from sqlalchemy import select, func, update
from cryptography.fernet import Fernet
from aiogram import Bot, Dispatcher
from aiogram.types import Message, Chat, Update, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
import pytest
from test_bot import database as database, register, person, FakeSession
from app.config.settings import Settings
from app.database.models import Ticket, TicketMessage, ChatNotice, Activity, Payment, Subscription, ForumThread, now
from app.database.repositories import SQLRepository
from app.services.tickets import TicketService
from app.services.control import ControlService, PAGES, PAGE_NAMES
from app.services.activity import prepare_history
from app.services.notices import NoticeWorker
from app.services.admin import AdminService
from app.bot.handlers import create_router
from app.bot.middlewares import ServicesMiddleware
from app.bot.states import TicketInput
from app.services.integrations import MockPaymentService, MockProvisioningService


def config():
    return Settings(_env_file=None,admin_ids='8066410491',encryption_key=Fernet.generate_key().decode())


class Sender:
    def __init__(self):
        self.calls=[]
        self.error=None
        self.topics=[]

    async def get_chat(self,*args):
        return SimpleNamespace(is_forum=True)

    async def create_forum_topic(self,target,**options):
        self.topics.append((target,options))
        return SimpleNamespace(message_thread_id=700+len(self.topics))

    async def reopen_forum_topic(self,*args):
        pass

    async def close_forum_topic(self,*args):
        pass

    async def edit_message_text(self,*args,**options):
        pass

    async def send_message(self,target,text,**options):
        self.calls.append((target,text,options))
        if self.error:
            raise self.error
        return SimpleNamespace(message_id=len(self.calls))

    async def send_photo(self,target,photo,**options):
        return await self.send_message(target,photo,**options)

    async def send_document(self,target,document,**options):
        return await self.send_message(target,document,**options)


async def test_russian_screen_labels_and_ticket_visibility(database):
    admin=await register(database,8066410491)
    client=await register(database,2)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        screen=await ControlService(repo,config()).render('admin:x:appearance',admin)
        assert set(PAGE_NAMES)==set(PAGES)
        assert {target for row in screen.rows[:-1] for _,target in row}=={'admin:x:appearance_screens','admin:buttons:0','admin:x:content','admin:texts'}
        assert all(any('А'<=c<='я' for c in label) for row in screen.rows[:-1] for label,_ in row)
        with pytest.raises(PermissionError):
            await TicketService(repo,config()).render('admin:t:home',client)
        with pytest.raises(PermissionError):
            await TicketService(repo,config()).bind(client,'support',-100123)




async def test_concurrent_new_messages_share_one_active_ticket(database):
    client=await register(database,2)
    async def send(id):
        async with database.sessions.begin() as session:
            await TicketService(SQLRepository(session),config()).input('t:input:new',client,'Question',f'2:{id}')
    await asyncio.gather(send(1),send(2))
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Ticket))==1
        assert await session.scalar(select(func.count()).select_from(TicketMessage))==2
