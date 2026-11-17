import asyncio
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import User as TelegramUser, Message, Chat, Update, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from fastapi.testclient import TestClient
import pytest
from app.config.settings import Settings
from app.database.session import Database
from app.database.repositories import SQLRepository
from app.database.models import Subscription, BalanceTransaction, now
from app.services.domain import seed, ReferralService, SubscriptionService, PromoService
from app.services.screens import ScreenService
from app.services.integrations import MockPaymentService, MockProvisioningService
from app.bot.keyboards import keyboard
from app.bot.handlers import create_router
from app.bot.middlewares import ServicesMiddleware
from app.main import create_app


def person(id):
    return TelegramUser(id=id, is_bot=False, first_name='Тест <User>')


@pytest.fixture
async def database(tmp_path):
    db = Database('sqlite+aiosqlite:///' + (tmp_path / 'test.db').as_posix())
    db.sessions.configure(info={'isolated_demo_provisioning':True})
    await db.initialize()
    async with db.sessions.begin() as session:
        await seed(SQLRepository(session), Settings(_env_file=None))
    yield db
    await db.close()


async def register(db, id, payload=''):
    async with db.sessions.begin() as session:
        return await ReferralService(SQLRepository(session)).register(person(id), payload)


async def test_referrals(database):
    inviter = await register(database, 100)
    invitee = await register(database, 200, 'ref_100')
    assert invitee.referrer_id == inviter.id
    other = await register(database, 300)
    again = await register(database, 200, 'ref_300')
    assert again.referrer_id == inviter.id
    self_user = await register(database, 400, 'ref_400')
    assert self_user.referrer_id is None
    missing = await register(database, 500, 'ref_999999')
    assert missing.referrer_id is None
    await register(database, 600)
    later = await register(database, 600, 'ref_100')
    assert later.referrer_id is None
    async with database.sessions() as session:
        assert await SQLRepository(session).referral_count(inviter.id) == 1


async def test_trial_atomic_and_persistent(database):
    user = await register(database, 1)
    async def activate():
        async with database.sessions.begin() as session:
            return await SubscriptionService(SQLRepository(session), 3).activate_trial(user.id)
    results = await asyncio.gather(activate(), activate())
    assert sum(s is not None for s in results) == 1
    second_db = Database(str(database.engine.url))
    async with second_db.sessions.begin() as session:
        repo = SQLRepository(session)
        saved = await repo.user(1)
        assert saved.trial_used
        assert await SubscriptionService(repo, 3).activate_trial(saved.id) is None
        assert len(await repo.subscriptions(saved.id)) == 1
    await second_db.close()


async def test_promo_and_ledger(database):
    user = await register(database, 1)
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        promos = PromoService(repo)
        assert await promos.apply(user.id, ' freeman10 ')
        assert await promos.discount(user.id) == 10
        assert await promos.apply(user.id, 'FREEMAN10') is None
        assert await promos.apply(user.id, 'invalid') is None
        assert await repo.balance(user.id) == 0
        await repo.add(BalanceTransaction(user_id=user.id, type='test_credit', amount=10000, description='Тест'))
        await repo.add(BalanceTransaction(user_id=user.id, type='test_debit', amount=-2500, description='Тест'))
        assert await repo.balance(user.id) == 7500
        screens = ScreenService(repo, Settings(_env_file=None), MockPaymentService(), MockProvisioningService())
        assert '900 ₽' in (await screens.render('tariff:3', user)).text




class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        from types import SimpleNamespace
        if method.__api_method__ == 'getChat':
            return SimpleNamespace(is_forum=True)
        if method.__api_method__ == 'getChatMember':
            return SimpleNamespace(status='administrator',can_manage_topics=True)
        if method.__api_method__ in ('sendMessage', 'editMessageText','sendPhoto','sendDocument','sendRichMessage','editMessageRich'):
            return Message(message_id=10, date=datetime.now(timezone.utc), chat=Chat(id=1, type='private'), text=getattr(method,'text',None),caption=getattr(method,'caption',None))
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b''




def test_health(tmp_path):
    settings = Settings(_env_file=None, bot_token='', database_url='sqlite+aiosqlite:///' + (tmp_path / 'health.db').as_posix())
    async def prepare():
        db = Database(settings.database_url)
        await db.initialize()
        await db.close()
    asyncio.run(prepare())
    with TestClient(create_app(settings)) as client:
        response = client.get('/health')
        assert response.status_code == 200
        assert response.json()['bot'] == 'token_missing'


async def test_expiry_and_other_users_subscription(database):
    owner = await register(database, 10)
    stranger = await register(database, 20)
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        subscription = Subscription(user_id=owner.id, kind='trial', expires_at=now() - timedelta(seconds=1), device_limit=3)
        await repo.add(subscription)
        screens = ScreenService(repo, Settings(_env_file=None), MockPaymentService(), MockProvisioningService())
        assert 'Истекла' in (await screens.render(f'sub:{subscription.id}', owner)).text
        for action in ['sub', 'connection', 'devices', 'device_add', 'history', 'renew', 'instructions']:
            assert 'недоступен' in (await screens.render(f'{action}:{subscription.id}', stranger)).text
        assert 'недоступен' in (await screens.render(f'tariff:3:{subscription.id}', stranger)).text


async def test_internal_error_is_logged_and_hidden(database, caplog):
    class BrokenPayment:
        async def checkout(self, user_id, amount):
            raise RuntimeError('TEST_INTERNAL_FAILURE')

    session = FakeSession()
    bot = Bot('123456:TEST_TOKEN_NOT_REAL', session=session)
    dispatcher = Dispatcher()
    router = create_router()
    router.callback_query.outer_middleware(ServicesMiddleware(database, Settings(_env_file=None), BrokenPayment(), MockProvisioningService()))
    dispatcher.include_router(router)
    query = CallbackQuery(id='error', from_user=person(1), chat_instance='test', data='pay:3',
                          message=Message(message_id=10, date=now(), chat=Chat(id=1, type='private')))
    await dispatcher.feed_update(bot, Update(update_id=1, callback_query=query))
    texts = [(getattr(call, 'text', '') or '') for call in session.calls]
    assert 'Что-то пошло не так. Попробуйте ещё раз.' in texts
    assert not any('TEST_INTERNAL_FAILURE' in text for text in texts)
    assert 'TEST_INTERNAL_FAILURE' in caplog.text
    await dispatcher.storage.close()
    await bot.session.close()
