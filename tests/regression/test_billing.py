import asyncio
from datetime import timedelta
from sqlalchemy import select,func
import pytest
from test_bot import database as database,register
from test_tickets import config
from app.database.models import AccessKey,Subscription,Payment,BalanceTransaction,ChatNotice,now
from app.database.repositories import SQLRepository
from app.services.billing import BillingService,prepare_autorenew
from app.services.dispatch import DispatchWorker
from app.services.screens import ScreenService
from app.services.integrations import MockPaymentService,MockProvisioningService
from app.services.admin import AdminService
from app.bot.keyboards import keyboard


async def setup_sub(repo,user):
    tariff=await repo.tariff(1)
    sub=await repo.add(Subscription(user_id=user.id,tariff_id=tariff.id,kind='paid',expires_at=now()-timedelta(seconds=1)))
    key=await repo.add(AccessKey(user_id=user.id,name='Телефон',expires_at=sub.expires_at))
    await repo.set_setting(f'subscription_key:{sub.id}',str(key.id))
    return sub,key


async def test_default_off_independent_renewal_and_worker_idempotence(database):
    user=await register(database,2)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        a,key=await setup_sub(repo,user);b,_=await setup_sub(repo,user)
        tariff=await repo.tariff(1);price=tariff.price
        await repo.add(BalanceTransaction(user_id=user.id,type='test',amount=price*3,description='Test'))
        await prepare_autorenew(repo)
        assert await repo.balance(user.id)==price*3
        await repo.set_setting(f'auto_renew:{a.id}','1')
    await asyncio.gather(DispatchWorker(database,None).prepare(),DispatchWorker(database,None).prepare())
    async with database.sessions() as session:
        repo=SQLRepository(session)
        assert await repo.balance(user.id)==price*2
        assert await session.scalar(select(func.count()).select_from(Payment))==1
        assert (await session.get(Subscription,a.id)).expires_at>now().replace(tzinfo=None)
        assert (await session.get(Subscription,b.id)).expires_at<now().replace(tzinfo=None)


async def test_insufficient_frozen_pending_and_deleted_never_charge(database):
    user=await register(database,2)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);sub,key=await setup_sub(repo,user)
        await repo.set_setting(f'auto_renew:{sub.id}','1')
        await prepare_autorenew(repo);await prepare_autorenew(repo)
        assert await repo.balance(user.id)==0
        assert await session.scalar(select(func.count()).select_from(ChatNotice))==1
        await repo.add(BalanceTransaction(user_id=user.id,type='test',amount=100000,description='Test'))
        for status in ('frozen','gift_pending','deleted'):
            key.status=status
            await session.flush()
            await prepare_autorenew(repo)
            assert await repo.balance(user.id)==100000
        assert await session.scalar(select(func.count()).select_from(Payment))==0


async def test_balance_purchase_atomic_replay_and_qr_card(database):
    user=await register(database,2)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);tariff=await repo.tariff(1)
        await repo.add(BalanceTransaction(user_id=user.id,type='test',amount=tariff.price*2,description='Test'))
        action=await BillingService(repo,config()).preview(user,1,0);price=tariff.price
    async def purchase():
        async with database.sessions.begin() as session:
            repo=SQLRepository(session)
            screen=await BillingService(repo,config()).render(action,user)
            assert screen.photo and not screen.document
            markup=keyboard(screen.rows)
            assert markup.inline_keyboard[0][0].text=='📲 Установить подписку'
            assert any(button.copy_text for row in markup.inline_keyboard for button in row)
    await asyncio.gather(purchase(),purchase())
    async with database.sessions() as session:
        repo=SQLRepository(session)
        assert await repo.balance(user.id)==price
        assert await session.scalar(select(func.count()).select_from(Payment))==1
        assert await session.scalar(select(func.count()).select_from(Subscription))==1


async def test_repricing_requires_new_confirmation_and_recipient_cannot_pay(database):
    user=await register(database,2);other=await register(database,3)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=BillingService(repo,config())
        await repo.add(BalanceTransaction(user_id=user.id,type='test',amount=100000,description='Test'))
        action=await service.preview(user,1,0)
        with pytest.raises(ValueError):
            await service.render(action,other)
        await repo.set_setting(f'client_price:{user.id}:1','12345')
        with pytest.raises(ValueError):
            await service.render(action,user)
        assert await repo.balance(user.id)==100000


async def test_manual_credit_admin_only_and_once(database):
    user=await register(database,2);admin=await register(database,8066410491)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=AdminService(repo,config())
        preview=await service.apply_input(f'admin:input:balance_credit:{user.id}','125.50',admin)
        action=preview.rows[0][0][1]
        with pytest.raises(PermissionError):
            await service.render(action,user)
        await service.render(action,admin);await service.render(action,admin)
        assert await repo.balance(user.id)==12550
