import asyncio
from sqlalchemy import select,func
import pytest
from test_bot import database as database,register
from test_tickets import config
from app.database.models import Payment,Subscription,AccessKey
from app.database.repositories import SQLRepository
from app.services.screens import ScreenService
from app.services.integrations import MockPaymentService,MockProvisioningService


async def test_admin_free_role_once_and_direct_key_listing(database):
    admin=await register(database,8066410491);client=await register(database,2)
    def screens(session):
        return ScreenService(SQLRepository(session),config(),MockPaymentService(),MockProvisioningService())
    async with database.sessions.begin() as session:
        screen=await screens(session).render('tariff:1',admin)
        action=screen.rows[0][0][1]
        assert 'бесплатно' in screen.text
        with pytest.raises(PermissionError):
            await screens(session).render(action,client)
        assert not any(target.startswith('admin_free:') for row in (await screens(session).render('tariff:1',client)).rows for _,target in row)
    async def confirm():
        async with database.sessions.begin() as session:
            await screens(session).render(action,admin)
    await asyncio.gather(confirm(),confirm())
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Subscription))==1
        assert await session.scalar(select(func.count()).select_from(AccessKey))==1
        assert await session.scalar(select(func.count()).select_from(Payment))==0
        listing=await screens(session).render('subscriptions',admin)
        keys=[(label,target) for row in listing.rows for label,target in row if target.startswith('keydetail:')]
        assert len(keys)==1 and 'дн.' in keys[0][0]
        assert not any(target.startswith(('keys:','sub:')) for row in listing.rows for _,target in row)
