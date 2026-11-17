import asyncio
from datetime import timedelta
from sqlalchemy import select,func
import pytest
from aiogram import Bot,Dispatcher
from aiogram.types import Message,Chat,Update
from aiogram.exceptions import TelegramBadRequest
from test_bot import database as database,register,FakeSession,person
from test_tickets import config
from test_billing import setup_sub
from app.database.repositories import SQLRepository
from app.database.models import Payment,BalanceTransaction,AccessKey,Subscription,now
from app.services.billing import BillingService,prepare_autorenew
from app.services.topups import TopupService
from app.bot.handlers import create_router
from app.bot.middlewares import ServicesMiddleware
from app.services.integrations import MockPaymentService,MockProvisioningService
from app.services.screens import ScreenService


async def test_start_works_when_telegram_forbids_edit_after_keyboard_removal(database):
    class StrictTelegram(FakeSession):
        async def make_request(self,bot,method,timeout=None):
            if method.__api_method__=='editMessageReplyMarkup':
                raise TelegramBadRequest(method=method,message="message can't be edited")
            return await super().make_request(bot,method,timeout)
    fake=StrictTelegram();bot=Bot('123456:TEST_TOKEN_NOT_REAL',session=fake)
    router=create_router();router.message.outer_middleware(ServicesMiddleware(database,config(),MockPaymentService(),MockProvisioningService()))
    dispatcher=Dispatcher();dispatcher.include_router(router)
    await dispatcher.feed_update(bot,Update(update_id=1,message=Message(message_id=1,date=now(),chat=Chat(id=2,type='private'),from_user=person(2),text='/start')))
    assert any(getattr(method,'reply_markup',None).__class__.__name__=='InlineKeyboardMarkup' for method in fake.calls)
    assert not any('Что-то пошло не так' in (getattr(method,'text','') or '') for method in fake.calls)
    await bot.session.close();await dispatcher.storage.close()


async def test_registration_survives_temporary_first_screen_failure(database):
    class FailingFirstScreen(FakeSession):
        failed=False
        async def make_request(self,bot,method,timeout=None):
            if method.__api_method__=='sendMessage' and not self.failed:
                self.failed=True
                raise TelegramBadRequest(method=method,message='temporary send failure')
            return await super().make_request(bot,method,timeout)
    fake=FailingFirstScreen();bot=Bot('123456:TEST_TOKEN_NOT_REAL',session=fake)
    router=create_router();router.message.outer_middleware(ServicesMiddleware(database,config(),MockPaymentService(),MockProvisioningService()))
    dispatcher=Dispatcher();dispatcher.include_router(router)
    await dispatcher.feed_update(bot,Update(update_id=2,message=Message(message_id=2,date=now(),chat=Chat(id=345,type='private'),from_user=person(345),text='/start')))
    async with database.sessions() as session:
        assert await SQLRepository(session).user(345) is not None
    await bot.session.close();await dispatcher.storage.close()




async def test_daily_does_not_shorten_purchase_blocks_then_recovers_without_backbilling(database):
    user=await register(database,2)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);sub,key=await setup_sub(repo,user)
        sub.expires_at=now()+timedelta(days=30);key.expires_at=sub.expires_at
        expiry=sub.expires_at
        await BillingService(repo,config()).render(f'daily:{sub.id}:on',user)
        await prepare_autorenew(repo)
        assert sub.expires_at==expiry and await repo.balance(user.id)==0
        sub.expires_at=now()-timedelta(days=10);key.expires_at=sub.expires_at
        await session.flush();await prepare_autorenew(repo)
        assert key.status=='billing_blocked' and await repo.balance(user.id)==0
        await repo.add(BalanceTransaction(user_id=user.id,type='test',amount=1000,description='Test'))
        await prepare_autorenew(repo);await prepare_autorenew(repo)
        assert key.status=='active' and await repo.balance(user.id)==500
        assert now()+timedelta(hours=23)<sub.expires_at<now()+timedelta(hours=25)
        assert await session.scalar(select(func.count()).select_from(Payment))==1
        await BillingService(repo,config()).render(f'daily:{sub.id}:off',user)
        paid_until=sub.expires_at;await prepare_autorenew(repo)
        assert sub.expires_at==paid_until


async def test_daily_not_for_trial_or_tariff_menu_or_other_customer(database):
    user=await register(database,2);other=await register(database,3)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);sub,key=await setup_sub(repo,user)
        service=BillingService(repo,config());sub.kind='trial'
        with pytest.raises(ValueError):
            await service.render(f'daily:{sub.id}:on',user)
        sub.kind='paid'
        with pytest.raises(ValueError):
            await service.render(f'daily:{sub.id}:on',other)
        screens=ScreenService(repo,config(),MockPaymentService(),MockProvisioningService())
        assert not any(t.startswith('daily:') for row in (await screens.render('buy',user)).rows for _,t in row)


def merchant_settings():
    settings=config();settings.yookassa_shop_id='test-shop';settings.yookassa_secret_key='TEST-NOT-REAL'
    return settings


class Provider:
    async def create(self,payment,key):
        self.key=key
        self.data=dict(id='test-payment',status='pending',paid=False,test=True,amount=dict(value=f'{payment.amount/100:.2f}',currency='RUB'),metadata=dict(freeman_payment=str(payment.id),freeman_user=str(payment.user_id)),confirmation=dict(confirmation_url='https://yoomoney.ru/test'))
        return self.data
    async def get(self,id):
        return self.data


async def test_yookassa_only_confirmed_matching_payments_credit_once(database):
    user=await register(database,2);other=await register(database,3);provider=Provider();settings=merchant_settings()
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=TopupService(repo,settings,provider)
        preview=await service.prepare(user,12345);action=preview.rows[0][0][1]
    async with database.sessions.begin() as session:
        service=TopupService(SQLRepository(session),settings,provider)
        with pytest.raises(ValueError):
            await service.render(action,other)
        await service.render(action,user)
        check=action.replace('topup_pay:','topup_check:')
        await service.render(check,user)
        assert await service.repo.balance(user.id)==0
        provider.data.update(status='succeeded',paid=True)
        provider.data['amount']['value']='1.00'
        with pytest.raises(ValueError):
            await service.render(check,user)
        assert await service.repo.balance(user.id)==0
        provider.data['amount']['value']='123.45'
    async def confirm():
        async with database.sessions.begin() as session:
            await TopupService(SQLRepository(session),settings,provider).render(check,user)
    await asyncio.gather(confirm(),confirm())
    async with database.sessions() as session:
        repo=SQLRepository(session)
        assert await repo.balance(user.id)==12345
        assert await session.scalar(select(func.count()).select_from(BalanceTransaction))==1
        assert (await session.scalar(select(Payment))).subscription_id is None
