from datetime import timedelta, timezone
from aiogram import Bot, Dispatcher
from aiogram.types import Update, Message, Chat, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from sqlalchemy import select, func
import pytest
from test_bot import database as database, register, person, FakeSession
from app.config.settings import Settings
from app.services.admin import AdminService
from app.services.screens import ScreenService
from app.services.domain import SubscriptionService, PromoService, seed, ReferralService
from app.services.integrations import MockPaymentService, MockProvisioningService
from app.database.repositories import SQLRepository
from app.database.models import AccessKey, AdminAudit, Payment, Server, now
from app.bot.handlers import create_router
from app.bot.middlewares import ServicesMiddleware
from app.bot.states import AdminInput
from app.bot.keyboards import keyboard


def config():
    return Settings(_env_file=None, admin_ids='8066410491')


async def test_role_visibility_and_service_denial(database):
    admin = await register(database, 8066410491)
    client = await register(database, 2)
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        screens = ScreenService(repo, config(), MockPaymentService(), MockProvisioningService())
        assert any(target == 'admin:home' for row in (await screens.render('home', admin)).rows for _, target in row)
        assert not any(target.startswith('admin:') for row in (await screens.render('home', client)).rows for _, target in row)
        for action in ['admin:home', 'admin:clients:0', f'admin:client:{admin.id}', 'admin:sales', 'admin:do:trialtoggle:0']:
            with pytest.raises(PermissionError):
                await screens.render(action, client)
        with pytest.raises(PermissionError):
            await AdminService(repo, config()).apply_input('admin:input:setting:trial_hours', '168', client)


async def test_admin_settings_applied_and_not_reset_on_start(database):
    admin = await register(database, 8066410491)
    client = await register(database, 2)
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        service = AdminService(repo, config())
        for action, value in [('setting:trial_hours', '168'), ('tariff_name:3', 'Свобода <3>'), ('tariff_price:3', '1500.50'), ('tariff_limit:3', '5'), ('button:buy', '🚀 Оформить доступ'), ('text:welcome', 'Добро пожаловать <друг>'), ('text:brand', 'Freeman Plus'), ('setting:referral_percent', '25'), ('text:support_url', 'https://t.me/support_test')]:
            await service.apply_input('admin:input:' + action, value, admin)
        screens = ScreenService(repo, config(), MockPaymentService(), MockProvisioningService())
        trial = await screens.render('activate', client)
        assert '168 ч.' in trial.text
        subscription = (await repo.subscriptions(client.id))[0]
        expiry = subscription.expires_at.replace(tzinfo=timezone.utc)
        assert timedelta(hours=167) < expiry - now() <= timedelta(hours=168)
        home = await screens.render('home', client)
        assert '&lt;друг&gt;' in home.text
        assert any(label == '🚀 Оформить доступ' for row in (await screens.render('home',client)).rows for label, _ in row)
        assert 'Свобода &lt;3&gt;' in (await screens.render('tariff:3', client)).text
        await seed(repo, Settings(_env_file=None))
        assert (await repo.tariff(3)).price == 150050
    async with database.sessions() as session:
        repo = SQLRepository(session)
        assert await repo.setting('trial_hours') == '168'
        assert (await repo.tariff(3)).device_limit == 5
        assert await session.scalar(select(func.count()).select_from(AdminAudit)) == 9


async def test_admin_edits_greeting_and_custom_button_appearance(database):
    admin=await register(database,8066410491);client=await register(database,2)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=AdminService(repo,config())
        texts=await service.render('admin:texts',admin)
        assert 'Оставь границы позади.' in texts.text
        await service.apply_input('admin:input:text:welcome','Добро пожаловать в новый мир.',admin)
        await service.apply_input('admin:input:button_icon:buy','5368324170671202286',admin)
        await service.render('admin:buttonstyle:buy:success',admin)
        screens=ScreenService(repo,config(),MockPaymentService(),MockProvisioningService())
        home=await screens.render('home',client)
        assert 'Добро пожаловать в новый мир.' in home.text
        assert home.button_options['buy']=={'icon_custom_emoji_id':'5368324170671202286','style':'success'}
        markup=keyboard(home.rows,home.button_options)
        button=next(button for row in markup.inline_keyboard for button in row if button.callback_data=='buy')
        assert button.icon_custom_emoji_id=='5368324170671202286' and button.style=='success'
        await service.apply_input('admin:input:button_icon:buy','-',admin)
        assert await repo.setting('button_icon:buy')==''






async def confirmed(service, operation, target, admin):
    screen = await service.render(f'admin:confirm:{operation}:{target}', admin)
    return screen.rows[0][0][1]




async def test_disabled_programs_promos_and_validation(database):
    admin = await register(database, 8066410491)
    client = await register(database, 2)
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        service = AdminService(repo, config())
        for action, value in [('setting:trial_hours', '0'), ('tariff_price:3', 'nan'), ('tariff_price:3', '1.001'), ('setting:referral_percent', '101'), ('key:999', 'name | 30'), ('button:admin', 'bad'), ('text:support_url', 'javascript:alert(1)'), ('promo', 'CODE | 101')]:
            with pytest.raises(ValueError):
                await service.apply_input('admin:input:' + action, value, admin)
        await service.render(await confirmed(service, 'trialtoggle', 0, admin), admin)
        assert await SubscriptionService(repo, 3).activate_trial(client.id) is None
        assert not (await repo.user(2)).trial_used
        await service.render(await confirmed(service, 'referraltoggle', 0, admin), admin)
        invitee = await ReferralService(repo).register(person(3), 'ref_2')
        assert invitee.referrer_id is None
        await service.render(await confirmed(service, 'tarifftoggle', 3, admin), admin)
        screens = ScreenService(repo, config(), MockPaymentService(), MockProvisioningService())
        assert all(target != 'tariff:3' for row in (await screens.render('buy', client)).rows for _, target in row)
        assert 'недоступен' in (await screens.render('tariff:3', client)).text
        await service.apply_input('admin:input:promo', 'NEW25 | 25', admin)
        assert await PromoService(repo).apply(client.id, 'NEW25')
        assert await PromoService(repo).discount(client.id) == 25
        await service.apply_input('admin:input:promo_toggle', 'NEW25', admin)
        assert await PromoService(repo).discount(client.id) == 0


async def test_admin_dispatcher_fsm_and_client_forgery(database):
    fake = FakeSession()
    bot = Bot('123456:TEST_TOKEN_NOT_REAL', session=fake)
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    router = create_router()
    middleware = ServicesMiddleware(database, config(), MockPaymentService(), MockProvisioningService())
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)
    dispatcher.include_router(router)
    async def send(id, text=None, action=None):
        message = Message(message_id=1, date=now(), chat=Chat(id=id, type='private'), from_user=person(id), text=text)
        if action:
            update = Update(update_id=1, callback_query=CallbackQuery(id='1', from_user=person(id), chat_instance='test', data=action, message=message))
        else:
            update = Update(update_id=1, message=message)
        await dispatcher.feed_update(bot, update)
    await send(8066410491, '/admin')
    await send(8066410491, action='admin:input:setting:trial_hours')
    await send(8066410491, '168')
    await send(2, '/admin')
    await send(2, action='admin:input:setting:trial_hours')
    await send(2, action='admin:do:trialtoggle:0')
    # Even a forged FSM state is denied on the input handler.
    context = dispatcher.fsm.get_context(bot=bot, chat_id=2, user_id=2)
    await context.set_state(AdminInput.waiting)
    await context.update_data(admin_action='admin:input:setting:trial_hours')
    await send(2, '1')
    async with database.sessions() as session:
        repo = SQLRepository(session)
        assert await repo.setting('trial_hours') == '168'
        assert await repo.setting('trial_enabled', '1') == '1'
        assert await session.scalar(select(func.count()).select_from(AdminAudit)) == 1
    assert not any('Что-то пошло не так' in (getattr(call, 'text', '') or '') for call in fake.calls)
    await dispatcher.storage.close()
    await dispatcher.fsm.events_isolation.close()
    await bot.session.close()


async def test_all_admin_navigation_paths(database):
    admin = await register(database, 8066410491)
    client = await register(database, 2)
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        await SubscriptionService(repo, 3).activate_trial(client.id)
        service = AdminService(repo, config())
        await service.apply_input(f'admin:input:key:{client.id}', 'test | 7', admin)
        queue, seen = ['admin:home'], set()
        while queue:
            action = queue.pop(0)
            if action in seen or action.startswith(('admin:do:', 'admin:keyapply:')):
                continue  # Mutation behavior is verified separately with confirmation.
            seen.add(action)
            screen = await service.render(action, admin)
            assert screen.text and screen.rows, action
            for row in screen.rows:
                for _, target in row:
                    if target.startswith('admin:'):
                        assert len(target.encode()) <= 64
                        queue.append(target)
        assert len(seen) >= 50
