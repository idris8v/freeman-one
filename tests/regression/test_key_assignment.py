from datetime import timedelta
from sqlalchemy import select, func
import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Message, Chat, CallbackQuery, Update
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from test_bot import database as database, register, person, FakeSession
from app.config.settings import Settings
from app.database.models import AccessKey, AdminAudit, now
from app.database.repositories import SQLRepository
from app.services.admin import AdminService
from app.services.keys import KeyService
from app.services.screens import ScreenService
from app.services.integrations import MockPaymentService, MockProvisioningService
from app.bot.handlers import create_router
from app.bot.middlewares import ServicesMiddleware


def settings():
    return Settings(_env_file=None, admin_ids='8066410491')


async def create_key(repo, user, **values):
    key = AccessKey(user_id=user.id, name=values.pop('name', 'Телефон <3>'),
                    expires_at=values.pop('expires_at', now() + timedelta(days=30)), **values)
    await repo.add(key)
    return key




async def test_stale_confirmation_cannot_overwrite_new_owner(database):
    admin = await register(database, 8066410491)
    previous = await register(database, 1)
    first = await register(database, 2)
    second = await register(database, 3)
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        key = await create_key(repo, previous)
        service = AdminService(repo, settings())
        first_action = (await service.render(f'admin:keyassign:{key.id}:{first.id}', admin)).rows[0][0][1]
        second_action = (await service.render(f'admin:keyassign:{key.id}:{second.id}', admin)).rows[0][0][1]
        await service.render(first_action, admin)
        with pytest.raises(ValueError, match='изменился'):
            await service.render(second_action, admin)
        assert key.user_id == first.id
        assert await session.scalar(select(func.count()).select_from(AdminAudit).where(AdminAudit.action == 'key_assign')) == 1


async def test_invalid_keys_customers_roles_and_tampered_confirmation(database):
    admin = await register(database, 8066410491)
    previous = await register(database, 1)
    customer = await register(database, 2)
    other = await register(database, 3)
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        service = AdminService(repo, settings())
        keys = KeyService(repo, settings())
        for values in [{'status': 'deleted'}, {'expires_at': now() - timedelta(days=1)}, {'mode': 'real'}]:
            key = await create_key(repo, previous, **values)
            with pytest.raises(ValueError):
                await service.render(f'admin:keyassign:{key.id}:{customer.id}', admin)
        key = await create_key(repo, previous)
        with pytest.raises(PermissionError):
            await keys.assign(customer, key.id, customer.id, previous.id, 'active')
        with pytest.raises(PermissionError):
            await service.apply_input(f'admin:input:key_id:{customer.id}', str(key.id), customer)
        preview = await service.apply_input(f'admin:input:key_id:{customer.id}', '#' + str(key.id), admin)
        action = preview.rows[0][0][1]
        with pytest.raises(ValueError):
            await service.render(action.replace(f':{customer.id}:', f':{other.id}:'), admin)
        with pytest.raises(ValueError):
            await service.render(f'admin:keyapply:{key.id}:{customer.id}:FAKE', admin)
        saved_customer = await repo.user(customer.telegram_id)
        saved_customer.status = 'blocked'
        with pytest.raises(ValueError):
            await service.render(action, admin)
        with pytest.raises(ValueError):
            await service.apply_input(f'admin:input:key:{customer.id}', 'Телефон | 30', admin)
        assert key.user_id == previous.id
        assert await session.scalar(select(func.count()).select_from(AdminAudit).where(AdminAudit.action == 'key_assign')) == 0




async def test_create_and_assign_existing_key_through_dispatcher(database):
    fake = FakeSession()
    bot = Bot('123456:TEST_TOKEN_NOT_REAL', session=fake)
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    router = create_router()
    middleware = ServicesMiddleware(database, settings(), MockPaymentService(), MockProvisioningService())
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)
    dispatcher.include_router(router)
    first = await register(database, 1)
    second = await register(database, 2)
    async def send(id, text=None, action=None):
        message = Message(message_id=1, date=now(), chat=Chat(id=id, type='private'), from_user=person(id), text=text)
        if action:
            update = Update(update_id=1, callback_query=CallbackQuery(id='1', from_user=person(id), chat_instance='test', data=action, message=message))
        else:
            update = Update(update_id=1, message=message)
        await dispatcher.feed_update(bot, update)
    await send(8066410491, '/admin')
    await send(8066410491, action=f'admin:issue:{first.id}')
    await send(8066410491, action=f'admin:input:key:{first.id}')
    await send(8066410491, 'Мой телефон | 30')
    async with database.sessions() as session:
        key = await session.scalar(select(AccessKey))
        assert key.user_id == first.id
    await send(8066410491, action=f'admin:input:key_id:{second.id}')
    await send(8066410491, '#' + str(key.id))
    preview_method = fake.calls[-1]
    action = preview_method.reply_markup.inline_keyboard[0][0].callback_data
    assert action.startswith('admin:keyapply:')
    await send(8066410491, action=action)
    await send(1, action=f'admin:keyassign:{key.id}:{first.id}')
    await send(1, action=f'keydetail:{key.id}:0')
    await send(2, action='keys:0')
    await send(2, action=f'keydetail:{key.id}:0')
    async with database.sessions() as session:
        assert (await session.get(AccessKey, key.id)).user_id == second.id
    assert not any('Что-то пошло не так' in (getattr(call, 'text', '') or '') for call in fake.calls)
    await dispatcher.storage.close()
    await dispatcher.fsm.events_isolation.close()
    await bot.session.close()
