import asyncio
from datetime import timedelta
from sqlalchemy import select, func, update
from cryptography.fernet import Fernet
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
from aiogram.methods import SendMessage
import pytest
from test_bot import database as database, register, FakeSession, person
from app.config.settings import Settings
from app.database.repositories import SQLRepository
from app.database.models import (Campaign, Delivery, Subscription, Payment, Withdrawal,
    ReferralReward, BalanceTransaction, now)
from app.services.control import ControlService
from app.services.dispatch import DispatchWorker
from app.services.screens import ScreenService
from app.services.integrations import MockPaymentService, MockProvisioningService


def config():
    return Settings(_env_file=None, admin_ids='8066410491', encryption_key=Fernet.generate_key().decode())


class BotStub:
    def __init__(self, error=None):
        self.calls=[]
        self.error=error

    async def send_message(self, id, text, **kwargs):
        self.calls.append((id,text))
        if self.error:
            raise self.error

    async def send_photo(self, id, photo, **kwargs):
        self.calls.append((id,photo))


async def test_campaign_audiences_confirmation_replay_and_cancel(database):
    admin=await register(database,8066410491)
    active=await register(database,2)
    frozen=await register(database,3)
    blocked=await register(database,4)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        await repo.add(Subscription(user_id=active.id,kind='paid',expires_at=now()+timedelta(days=10)))
        sub=await repo.add(Subscription(user_id=frozen.id,kind='paid',expires_at=now()+timedelta(days=10)))
        await repo.set_setting(f'subscription_status:{sub.id}','frozen')
        (await repo.user(blocked.telegram_id)).status='blocked'
        service=ControlService(repo,config())
        assert [u.id for u in await service.audience('active')]==[active.id]
        assert {u.id for u in await service.audience('inactive')}=={admin.id,frozen.id}
        screen=await service.input('admin:x:input:campaign:all',admin,'<Hello>')
        campaign=await session.scalar(select(Campaign))
        assert '&lt;Hello&gt;' in screen.text
        assert await session.scalar(select(func.count()).select_from(Delivery))==0
        await service.render(f'admin:x:send:{campaign.id}',admin)
        await service.render(f'admin:x:send:{campaign.id}',admin)
        assert await session.scalar(select(func.count()).select_from(Delivery))==3
        await service.render(f'admin:x:cancel:{campaign.id}',admin)
        assert set(await session.scalars(select(Delivery.status)))=={'cancelled'}
        with pytest.raises(PermissionError):
            await service.render('admin:x:home',active)
        with pytest.raises(PermissionError):
            await service.input('admin:x:input:campaign:all',active,'forged')


async def test_delivery_retry_lease_and_blocked(database):
    client=await register(database,2)
    async with database.sessions.begin() as session:
        await SQLRepository(session).add(Delivery(dedup='one',user_id=client.id,text='Hi'))
    bot=BotStub(TelegramRetryAfter(method=SendMessage(chat_id=2,text='Hi'),message='limit',retry_after=2))
    worker=DispatchWorker(database,bot)
    assert await worker.once()
    async with database.sessions.begin() as session:
        job=await session.scalar(select(Delivery))
        assert job.status=='pending' and job.attempts==1
        job.available_at=now()-timedelta(seconds=1)
        job.status='sending'  # Recover a worker interrupted after taking its lease.
    bot.error=None
    worker.pause_until=0
    assert await worker.once()
    assert not await worker.once()
    async with database.sessions.begin() as session:
        assert (await session.scalar(select(Delivery))).status=='sent'
        (await session.get(type(client),client.id)).status='blocked'
        await SQLRepository(session).add(Delivery(dedup='two',user_id=client.id,text='skip'))
    assert await worker.once()
    assert len(bot.calls)==2
    async with database.sessions() as session:
        assert (await session.scalar(select(Delivery).where(Delivery.dedup=='two'))).status=='skipped'


async def test_reminders_dedup_renewal_and_optout(database):
    client=await register(database,2)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        sub=await repo.add(Subscription(user_id=client.id,kind='paid',expires_at=now()+timedelta(days=2)))
        await repo.add(Subscription(user_id=client.id,kind='trial',expires_at=now()+timedelta(hours=20)))
        id=sub.id
    bot=BotStub()
    worker=DispatchWorker(database,bot)
    await worker.prepare(); await worker.prepare()
    async with database.sessions.begin() as session:
        assert await session.scalar(select(func.count()).select_from(Delivery))==1
        (await session.get(Subscription,id)).expires_at=now()+timedelta(days=10)
    await worker.once()
    assert not bot.calls
    async with database.sessions.begin() as session:
        (await session.get(Subscription,id)).expires_at=now()+timedelta(days=1)
    await worker.prepare()
    async with database.sessions.begin() as session:
        await SQLRepository(session).set_setting(f'notifications:{client.id}','0')
    await worker.once()
    assert not bot.calls


async def test_withdraw_encryption_reserve_replay_rejection(database):
    admin=await register(database,8066410491)
    client=await register(database,2)
    outsider=await register(database,3)
    cfg=config()
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        await repo.add(BalanceTransaction(user_id=client.id,type='test_credit',amount=150000,description='credit'))
        service=ControlService(repo,cfg)
        await service.input('x:input:withdraw',client,'Bank | +79991234567 | Ivan Ivanov')
        w=await session.scalar(select(Withdrawal))
        assert '+79991234567' not in w.details and 'Ivan' not in w.details
        assert await repo.balance(client.id)==150000
        with pytest.raises(ValueError):
            await service.render(f'x:withdrawapply:{w.id}',outsider)
        await service.render(f'x:withdrawapply:{w.id}',client)
        await service.render(f'x:withdrawapply:{w.id}',client)
        assert await repo.balance(client.id)==0
        await service.render(f'admin:x:payoutapply:{w.id}:rejected',admin)
        await service.render(f'admin:x:payoutapply:{w.id}:rejected',admin)
        assert await repo.balance(client.id)==150000
        assert w.status=='rejected'
        assert await session.scalar(select(func.count()).select_from(Delivery).where(Delivery.dedup.like('payout:%')))==1


async def test_concurrent_withdrawals_cannot_overspend(database):
    client=await register(database,2)
    cfg=config()
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        await repo.add(BalanceTransaction(user_id=client.id,type='test_credit',amount=100000,description='credit'))
        service=ControlService(repo,cfg)
        await service.input('x:input:withdraw',client,'Bank | +79991234567 | Ivan')
        await service.input('x:input:withdraw',client,'Bank | +79991234567 | Ivan')
        ids=list(await session.scalars(select(Withdrawal.id)))
    async def apply(id):
        async with database.sessions.begin() as session:
            try:
                await ControlService(SQLRepository(session),cfg).render(f'x:withdrawapply:{id}',client)
                return True
            except ValueError:
                return False
    assert sum(await asyncio.gather(*(apply(id) for id in ids)))==1
    async with database.sessions() as session:
        assert await SQLRepository(session).balance(client.id)==0
        assert await session.scalar(select(func.count()).select_from(Withdrawal).where(Withdrawal.status=='pending'))==1


async def test_referral_rewards_once_first_purchase_and_refund(database):
    inviter=await register(database,100)
    invitee=await register(database,200,'ref_100')
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        p=await repo.add(Payment(user_id=invitee.id,status='succeeded',amount=100000))
        id=p.id
        await repo.add(Payment(user_id=invitee.id,status='succeeded',amount=40000))
        await repo.add(Payment(user_id=invitee.id,status='pending',amount=900000))
    worker=DispatchWorker(database,BotStub())
    await worker.prepare(); await worker.prepare()
    async with database.sessions.begin() as session:
        assert await SQLRepository(session).balance(inviter.id)==17000
        assert await session.scalar(select(func.count()).select_from(ReferralReward))==2
        (await session.get(Payment,id)).status='refunded'
    await worker.prepare(); await worker.prepare()
    async with database.sessions() as session:
        assert await SQLRepository(session).balance(inviter.id)==2000








async def test_refunded_revenue_is_subtracted_once_and_csv(database):
    admin=await register(database,8066410491)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        await repo.add(Payment(user_id=admin.id,status='refunded',amount=100000))
        service=ControlService(repo,config())
        text=(await service.report(7)).text
        assert 'Результат до налогов: 0 ₽' in text
        export=await service.render('admin:x:export:7',admin)
        assert 'refunded;1000.00' in export.document[0].decode('utf-8-sig')


async def test_management_exports_include_clients_keys_and_excel_sheets(database):
    from io import BytesIO
    from openpyxl import load_workbook
    from app.database.models import AccessKey
    admin=await register(database,8066410491)
    client=await register(database,22)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        await repo.add(AccessKey(user_id=client.id,name='Телефон',expires_at=now()+timedelta(days=30)))
        service=ControlService(repo,config())
        clients=await service.render('admin:x:exportclients:0',admin)
        keys=await service.render('admin:x:exportkeys:0',admin)
        excel=await service.render('admin:x:exportxlsx:0',admin)
        assert '22;' in clients.document[0].decode('utf-8-sig')
        assert 'Телефон' in keys.document[0].decode('utf-8-sig')
        workbook=load_workbook(BytesIO(excel.document[0]),read_only=True)
        assert workbook.sheetnames==['Клиенты','Подписки','Ключи','Платежи','По тарифам','По дням']
