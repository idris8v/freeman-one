from sqlalchemy import select,func
from aiogram import Bot,Dispatcher
from aiogram.types import Message,Chat,Update
from aiogram.fsm.storage.memory import MemoryStorage
from test_bot import database as database,register,person,FakeSession
from test_tickets import config
from app.database.models import TicketMessage,now
from app.database.repositories import SQLRepository
from app.services.tickets import TicketService
from app.bot.handlers import create_router
from app.bot.middlewares import ServicesMiddleware
from app.services.integrations import MockPaymentService,MockProvisioningService
