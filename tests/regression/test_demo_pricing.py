import asyncio
from sqlalchemy import select,func
import pytest
from test_bot import database as database,register
from test_tickets import config
from app.database.repositories import SQLRepository
from app.database.models import Subscription,AccessKey,Gift
from app.services.screens import ScreenService
from app.services.domain import SubscriptionService
from app.services.integrations import MockPaymentService,MockProvisioningService
from app.services.admin import AdminService
from app.services.gifts import GiftService
from app.services.pricing import quote


async def test_once_demo_cohort_file_gift_replacement_and_no_future_users(database):
    user=await register(database,2);recipient=await register(database,3)
    async with database.sessions.begin() as session:
        await SQLRepository(session).set_setting(f'demo_trial:{user.id}','pending')
    async def activate():
        async with database.sessions.begin() as session:
            return await SubscriptionService(SQLRepository(session),3).activate_trial(user.id)
    results=await asyncio.gather(activate(),activate())
    assert sum(s is not None for s in results)==1
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);screens=ScreenService(repo,config(),MockPaymentService(),MockProvisioningService())
        sub=await session.scalar(select(Subscription).where(Subscription.user_id==user.id))
        key=await session.scalar(select(AccessKey))
        screen=await screens.render(f'demo_download:{key.id}:{sub.id}',user)
        assert screen.document and b'DEMO-' in screen.document[0]
        assert screen.document[1].endswith('.conf')
        link=await screens.render(f'demo_download:{key.id}:{sub.id}:link',user)
        qr=await screens.render(f'demo_download:{key.id}:{sub.id}:qr',user)
        assert 'https://example.invalid/' in link.text
        assert qr.photo[0].startswith(b'\x89PNG\r\n\x1a\n')
        extra=await screens.render(f'subscription_extra:{sub.id}',user)
        assert not any(target==f'g:input:subscription:{sub.id}' for row in extra.rows for _,target in row)
        assert any(target==f'g:input:subscription:{sub.id}' for row in qr.rows for _,target in row)
        assert 'недоступен' in (await screens.render(f'demo_download:{key.id}:{sub.id}',recipient)).text
        giftservice=GiftService(repo,config())
        await giftservice.input(f'g:input:subscription:{sub.id}',user,str(recipient.telegram_id))
        gift=await session.scalar(select(Gift));await giftservice.confirm(gift,user)
        received=await giftservice.claim(gift.code,recipient)
        assert received.photo and received.photo[0]!=qr.photo[0]
        assert 'недоступен' in (await screens.render(f'demo_download:{key.id}:{sub.id}',user)).text
        for format in ('link','qr','config'):
            assert 'недоступен' in (await screens.render(f'demo_download:{key.id}:{sub.id}:{format}',recipient)).text
            assert 'недоступен' in (await screens.render(f'demo_download:{key.id}:{sub.id}:{format}',user)).text
        assert await repo.setting(f'demo_trial:{user.id}')=='used'
    future=await register(database,4)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        sub=await SubscriptionService(repo,3).activate_trial(future.id)
        # Every new trial now receives the complete QR/link/configuration panel.
        key_id=await repo.setting(f'subscription_key:{sub.id}')
        assert key_id
        assert await session.scalar(select(func.count()).select_from(AccessKey).where(AccessKey.user_id==future.id))==1
        screen=await ScreenService(repo,config(),MockPaymentService(),MockProvisioningService()).render(f'sub:{sub.id}',future)
        assert screen.photo and any(target.startswith('copy:') for row in screen.rows for _,target in row)


async def test_personal_discount_price_precedence_and_roles(database):
    admin=await register(database,8066410491);client=await register(database,2)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=AdminService(repo,config());tariff=await repo.tariff(1)
        with pytest.raises(PermissionError):
            await service.apply_input(f'admin:input:client_discount:{client.id}','90',client)
        await service.apply_input(f'admin:input:client_discount:{client.id}','25',admin)
        assert (await quote(repo,client.id,tariff))[0]==tariff.price*75//100
        await service.apply_input(f'admin:input:client_price:{client.id}:1','123.45',admin)
        assert await quote(repo,client.id,tariff)==(12345,69,True)
        await service.apply_input(f'admin:input:client_price:{client.id}:1','-',admin)
        assert (await quote(repo,client.id,tariff))[0]==tariff.price*75//100
        with pytest.raises(ValueError):
            await service.apply_input(f'admin:input:client_discount:{client.id}','101',admin)
