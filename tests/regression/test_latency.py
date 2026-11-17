import asyncio
from aiogram import Bot,Dispatcher
from aiogram.types import Message,Chat,Update,CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from test_bot import database as database,register,person,FakeSession
from test_tickets import config
from app.database.models import now
from app.bot.handlers import create_router
from app.bot.middlewares import ServicesMiddleware
from app.services.integrations import MockPaymentService,MockProvisioningService


def setup(database,fake):
    bot=Bot('123456:TEST_TOKEN_NOT_REAL',session=fake)
    dispatcher=Dispatcher();router=create_router()
    middleware=ServicesMiddleware(database,config(),MockPaymentService(),MockProvisioningService())
    router.message.outer_middleware(middleware);router.callback_query.outer_middleware(middleware)
    dispatcher.include_router(router)
    return bot,dispatcher




async def test_expired_callback_ack_does_not_abort_menu_navigation(database):
    class Expired(FakeSession):
        async def make_request(self,bot,method,timeout=None):
            if method.__api_method__=='answerCallbackQuery':
                raise TelegramBadRequest(method=method,message='query is too old and response timeout expired or query ID is invalid')
            return await super().make_request(bot,method,timeout)
    fake=Expired();bot,dispatcher=setup(database,fake)
    message=Message(message_id=1,date=now(),chat=Chat(id=2,type='private'),from_user=person(2),text='Menu')
    query=CallbackQuery(id='old',from_user=person(2),chat_instance='test',message=message,data='buy')
    await dispatcher.feed_update(bot,Update(update_id=1,callback_query=query))
    assert any(call.__api_method__=='editMessageText' for call in fake.calls)
    assert not any('Что-то пошло не так' in (getattr(call,'text','') or '') for call in fake.calls)
    await dispatcher.storage.close();await bot.session.close()
