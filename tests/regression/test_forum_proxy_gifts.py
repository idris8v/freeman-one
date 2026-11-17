import asyncio
from datetime import timedelta
from cryptography.fernet import Fernet
from sqlalchemy import select, func
import pytest
from test_bot import database as database, register
from test_tickets import Sender
from app.config.settings import Settings
from app.database.models import Gift, AccessKey, BalanceTransaction, Payment, Subscription, ProxyEndpoint, ProxyVisit, Ticket, ForumThread, ChatNotice, now
from app.database.repositories import SQLRepository
from app.services.gifts import GiftService
from app.services.proxies import ProxyService
from app.services.tickets import TicketService
from app.services.notices import NoticeWorker
from app.services.activity import prepare_history


def settings():
    return Settings(_env_file=None,admin_ids='8066410491',encryption_key=Fernet.generate_key().decode())


async def test_proxy_subscription_gating_encryption_and_distinct_metrics(database):
    admin=await register(database,8066410491);client=await register(database,2)
    config=settings();secret='ab'*16
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=ProxyService(repo,config)
        with pytest.raises(PermissionError):
            await service.input('admin:p:input:add',client,'Forbidden')
        await service.input('admin:p:input:add',admin,f'Первый | https://t.me/proxy?server=example.com&port=443&secret={secret}')
        item=await session.scalar(select(ProxyEndpoint))
        assert secret not in item.secret and secret in service.url(item)
        assert '30 ₽' in (await service.render('p:home',client)).text
        with pytest.raises(ValueError):
            await service.render(f'p:open:{item.id}',client)
        sub=await repo.add(Subscription(user_id=client.id,kind='paid',expires_at=now()+timedelta(days=1)))
        for _ in range(2):
            await service.render(f'p:open:{item.id}',client)
        visit=await session.scalar(select(ProxyVisit))
        assert visit.requests==2
        assert await session.scalar(select(func.count()).select_from(ProxyVisit))==1
        screen=await service.render(f'admin:p:item:{item.id}',admin)
        assert 'Получили ссылку: 1' in screen.text and 'не связана с Telemt API' in screen.text
        await repo.set_setting(f'subscription_status:{sub.id}','frozen')
        assert not await service.eligible(client)


async def test_gift_concurrent_confirmation_debits_once_and_is_not_new_cash(database):
    sender=await register(database,1);recipient=await register(database,2);config=settings()
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        await repo.add(BalanceTransaction(user_id=sender.id,type='credit',amount=100000,description='Credit'))
        tariff=await repo.tariff(1);price=tariff.price
        await GiftService(repo,config).input('g:input:buy:1',sender,'2')
        id=await session.scalar(select(Gift.id))
    async def confirm():
        async with database.sessions.begin() as session:
            service=GiftService(SQLRepository(session),config)
            await service.confirm(await session.get(Gift,id),sender)
    await asyncio.gather(confirm(),confirm())
    async with database.sessions.begin() as session:
        gift=await session.get(Gift,id)
        assert gift.status=='ready'
        assert await session.scalar(select(func.count()).select_from(Subscription))==0
        await GiftService(SQLRepository(session),config).claim(gift.code,recipient)
    async with database.sessions() as session:
        repo=SQLRepository(session)
        assert await repo.balance(sender.id)==100000-price
        assert await session.scalar(select(func.count()).select_from(Subscription).where(Subscription.user_id==recipient.id))==1
        payment=await session.scalar(select(Payment))
        assert payment.status=='balance_paid'
        assert (await session.get(Gift,id)).status=='claimed'


async def test_unregistered_gift_id_bound_claim_and_cancel_refund(database):
    sender=await register(database,1);stranger=await register(database,3);config=settings()
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=GiftService(repo,config)
        await repo.add(BalanceTransaction(user_id=sender.id,type='credit',amount=100000,description='Credit'))
        await service.input('g:input:buy:1',sender,'2')
        gift=await session.scalar(select(Gift));code=gift.code
        await service.confirm(gift,sender)
        assert gift.status=='ready'
        with pytest.raises(ValueError):
            await service.claim(code,stranger)
        await service.cancel(gift,sender);await service.cancel(gift,sender)
        assert await repo.balance(sender.id)==100000
    recipient=await register(database,2)
    async with database.sessions.begin() as session:
        service=GiftService(SQLRepository(session),config)
        with pytest.raises(ValueError):
            await service.claim(code,recipient)
        await service.input('g:input:buy:1',sender,'',contact_id=recipient.telegram_id)
        gift=await session.scalar(select(Gift).order_by(Gift.id.desc()))
        await service.confirm(gift,sender)
        await service.claim(gift.code,recipient)
        assert gift.status=='claimed'


async def test_key_gift_preserves_expiry_and_rejects_other_owner(database):
    sender=await register(database,1);recipient=await register(database,2);config=settings()
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=GiftService(repo,config)
        key=await repo.add(AccessKey(user_id=sender.id,name='Мама',expires_at=now()+timedelta(days=5)))
        expiry=key.expires_at
        old_sub=await repo.add(Subscription(user_id=sender.id,kind='paid',expires_at=expiry,device_limit=2))
        await repo.set_setting(f'subscription_key:{old_sub.id}',str(key.id))
        with pytest.raises(ValueError):
            await service.input(f'g:input:key:{key.id}',recipient,'1')
        await service.input(f'g:input:key:{key.id}',sender,'2')
        gift=await session.scalar(select(Gift));await service.confirm(gift,sender)
        assert gift.status=='ready'
        await service.claim(gift.code,recipient)
        await session.refresh(key)
        assert key.user_id==sender.id and key.status=='deleted'
        replacement=await session.scalar(select(AccessKey).where(AccessKey.user_id==recipient.id))
        assert replacement.id!=key.id and replacement.status=='active'
        assert replacement.expires_at.replace(tzinfo=expiry.tzinfo)==expiry
        assert await repo.setting(f'subscription_status:{old_sub.id}')=='frozen'
        assert await repo.setting(f'subscription_transferred:{old_sub.id}')=='1'
        await service.claim(gift.code,recipient)
        assert await session.scalar(select(func.count()).select_from(AccessKey).where(AccessKey.user_id==recipient.id))==1




async def test_history_default_excludes_support_but_retains_archive(database):
    admin=await register(database,8066410491);client=await register(database,2);config=settings()
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=TicketService(repo,config)
        await service.bind(admin,'history',-100456)
        await service.input('t:input:new',client,'Question','2:1')
        await prepare_history(repo)
        notices=list(await session.scalars(select(ChatNotice).where(ChatNotice.kind=='history')))
        assert notices and all('Тикет' not in item.text for item in notices)
