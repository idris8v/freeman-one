import pytest
from cryptography.fernet import Fernet
from aiogram.methods import SendRichMessage
from aiogram.types import PreCheckoutQuery, SuccessfulPayment, InputRichMessage
from sqlalchemy import select, func

from test_bot import database as database, register, person
from test_control import config
from app.config.settings import Settings
from app.database.models import Payment, Subscription, RemnawaveServer
from app.database.repositories import SQLRepository
from app.services.admin import AdminService
from app.services.control import ControlService
from app.services.domain import SubscriptionService
from app.services.integrations import MockPaymentService, MockProvisioningService
from app.services.payment_settings import PaymentSettings
from app.services.screens import ScreenService
from app.services.stars import StarService
from app.bot.handlers.navigation import input_rich
from app.bot.keyboards import keyboard


async def test_stars_checkout_is_verified_and_idempotent(database):
    client = await register(database, 42)
    settings = Settings(_env_file=None)
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        await repo.set_setting('payment_provider:stars', '1')
        await repo.set_setting('stars_price:1', '75')
        service = StarService(repo, settings)
        invoice = await service.create(client, 1)
        assert invoice['currency'] == 'XTR' and invoice['prices'][0].amount == 75
        query = PreCheckoutQuery(id='pre-1', from_user=person(42), currency='XTR', total_amount=75, invoice_payload=invoice['payload'])
        assert await service.precheckout(query, client)
        paid = SuccessfulPayment(currency='XTR', total_amount=75, invoice_payload=invoice['payload'],
                                 telegram_payment_charge_id='tg-charge-1', provider_payment_charge_id='')
        result = await service.complete(paid, client)
        assert result.photo and 'Как подключить' in result.text
        replay = await service.complete(paid, client)
        assert replay.photo
        payment = await session.scalar(select(Payment))
        assert payment.status == 'stars_paid' and payment.provider_id == 'stars:tg-charge-1'
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 1


async def test_key_card_balance_device_rotation_and_rich_report(database):
    admin = await register(database, 8066410491)
    client = await register(database, 43)
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        sub = await SubscriptionService(repo, 1).activate_trial(client.id)
        screens = ScreenService(repo, Settings(_env_file=None), MockPaymentService(), MockProvisioningService())
        home = await screens.render('home', client)
        balance_labels = [label for row in home.rows for label, target in row if target == 'balance']
        assert balance_labels and 'Баланс' in balance_labels[0] and '0 ₽' in balance_labels[0]
        await repo.add(__import__('app.database.models',fromlist=['BalanceTransaction']).BalanceTransaction(user_id=client.id,type='test',amount=300,description='test'))
        home = await screens.render('home', client)
        balance_labels = [label for row in home.rows for label, target in row if target == 'balance']
        assert balance_labels and 'Баланс' in balance_labels[0] and '3 ₽' in balance_labels[0]
        card = await screens.render(f'sub:{sub.id}', client)
        labels = [label for row in card.rows for label, _ in row]
        assert labels.count('📲 Установить подписку') == 1
        assert any('устройство' in label.lower() for label in labels)
        old = await repo.setting(f'demo_file:{await repo.setting(f"subscription_key:{sub.id}")}')
        rotated = await screens.render(f'device_rotate_apply:{sub.id}', client)
        new = await repo.setting(f'demo_file:{await repo.setting(f"subscription_key:{sub.id}")}')
        assert old != new and rotated.photo
        report = await ControlService(repo, config()).report(30)
        funnel = await ControlService(repo, config()).funnel(30)
        assert report.rich is not None and funnel.rich is not None
        for screen in (report, funnel):
            payload = input_rich(screen.rich)
            assert isinstance(payload, InputRichMessage)
            request = SendRichMessage(chat_id=admin.telegram_id, rich_message=payload, reply_markup=keyboard(screen.rows))
            assert request.reply_markup.inline_keyboard


async def test_payment_credentials_are_configured_in_bot_and_encrypted(database):
    key = Fernet.generate_key().decode()
    live = Settings(_env_file=None, encryption_key=key)
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        service = PaymentSettings(repo, live)
        await service.save({
            'yookassa_shop_id': 'shop-123',
            'yookassa_secret_key': 'secret-value',
            'yookassa_test_mode': False,
        })
        stored = await repo.setting('payment_secret:yookassa_secret_key')
        assert stored and 'secret-value' not in stored
        restored = Settings(_env_file=None, encryption_key=key)
        await PaymentSettings(repo, restored).hydrate()
        assert restored.yookassa_shop_id == 'shop-123'
        assert restored.yookassa_secret_key == 'secret-value'
        assert restored.yookassa_test_mode is False
