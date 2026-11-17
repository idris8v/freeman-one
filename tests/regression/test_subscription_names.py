from datetime import timedelta
from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from sqlalchemy import select
import pytest
from test_bot import database as database, register, person, FakeSession
from app.config.settings import Settings
from app.database.models import Subscription, now
from app.database.repositories import SQLRepository
from app.services.domain import SubscriptionService
from app.services.screens import ScreenService
from app.services.admin import AdminService
from app.services.integrations import MockPaymentService, MockProvisioningService
from app.bot.handlers import create_router
from app.bot.middlewares import ServicesMiddleware
from app.bot.keyboards import keyboard
from app.bot.states import SubscriptionNameInput




async def test_rename_validation_and_ownership(database):
    owner = await register(database, 1)
    stranger = await register(database, 2)
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        service = SubscriptionService(repo, 3)
        subscription = await service.activate_trial(owner.id)
        for name in ['', ' ' * 10, 'X' * 61, 'Мама\nПапа', 'Мама\x00']:
            with pytest.raises(ValueError):
                await service.rename(owner.id, subscription.id, name)
        with pytest.raises(ValueError):
            await service.rename(stranger.id, subscription.id, 'Чужая')
        screens = ScreenService(repo, Settings(_env_file=None), MockPaymentService(), MockProvisioningService())
        assert 'недоступен' in (await screens.render(f'rename:{subscription.id}', stranger)).text
        assert await repo.setting(f'subscription_name:{subscription.id}') == ''


async def test_rename_fsm_retry_cancel_and_forgery(database):
    fake = FakeSession()
    bot = Bot('123456:TEST_TOKEN_NOT_REAL', session=fake)
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    router = create_router()
    middleware = ServicesMiddleware(database, Settings(_env_file=None), MockPaymentService(), MockProvisioningService())
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)
    dispatcher.include_router(router)
    async def send(id, text=None, action=None):
        message = Message(message_id=1, date=now(), chat=Chat(id=id, type='private'), from_user=person(id), text=text)
        if action:
            event = CallbackQuery(id='1', from_user=person(id), chat_instance='test', data=action, message=message)
            update = Update(update_id=1, callback_query=event)
        else:
            update = Update(update_id=1, message=message)
        await dispatcher.feed_update(bot, update)
    await send(1, '/start')
    await send(1, action='activate')
    async with database.sessions() as session:
        subscription = await session.scalar(select(Subscription))
        sid = subscription.id
    await send(1, action=f'rename:{sid}')
    await send(1, 'X' * 61)
    context = dispatcher.fsm.get_context(bot=bot, chat_id=1, user_id=1)
    assert await context.get_state() == SubscriptionNameInput.waiting.state
    await send(1, 'Мама')
    assert await context.get_state() is None
    await send(1, action=f'rename:{sid}')
    await send(1, action=f'sub:{sid}')
    assert await context.get_state() is None
    await send(1, action=f'rename:{sid}')
    await send(1, '/start')
    assert await context.get_state() is None
    await send(2, action=f'rename:{sid}')
    foreign = dispatcher.fsm.get_context(bot=bot, chat_id=2, user_id=2)
    assert await foreign.get_state() is None
    await foreign.set_state(SubscriptionNameInput.waiting)
    await foreign.update_data(subscription_id=sid)
    await send(2, 'Чужая')
    async with database.sessions() as session:
        assert await SQLRepository(session).setting(f'subscription_name:{sid}') == 'Мама'
    assert not any('Что-то пошло не так' in (getattr(call, 'text', '') or '') for call in fake.calls)
    await dispatcher.storage.close()
    await dispatcher.fsm.events_isolation.close()
    await bot.session.close()


async def test_admin_contact_url_uses_telegram_id_without_username(database):
    admin = await register(database, 8066410491)
    client = await register(database, 2)
    async with database.sessions() as session:
        service = AdminService(SQLRepository(session), Settings(_env_file=None, admin_ids='8066410491'))
        screen = await service.render(f'admin:client:{client.id}', admin)
        button = keyboard(screen.rows).inline_keyboard[0][0]
        assert button.url == 'tg://user?id=2'
        assert button.callback_data is None
        with pytest.raises(PermissionError):
            await service.render(f'admin:client:{client.id}', client)
