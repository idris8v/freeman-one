from datetime import timedelta, datetime, timezone
import pytest
from test_bot import database as database, register
from test_tickets import config
from app.database.models import AccessKey, Subscription, now
from app.database.repositories import SQLRepository
from app.services.client_keys import ClientKeyService, prepare_client_thaws
from app.services.control import ControlService
from app.services.demo_keys import create_demo_file
from app.services.screens import ScreenService
from app.services.integrations import MockPaymentService, MockProvisioningService
from app.services.dispatch import DispatchWorker


async def setup(repo,user):
    sub=await repo.add(Subscription(user_id=user.id,kind='paid',expires_at=now()+timedelta(days=30)))
    key=await repo.add(AccessKey(user_id=user.id,name='Телефон',expires_at=sub.expires_at))
    await repo.set_setting(f'subscription_key:{sub.id}',str(key.id))
    await create_demo_file(repo,key)
    return sub,key


async def test_gift_button_is_single_and_not_repeated_in_extras(database):
    user=await register(database,2)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);sub,key=await setup(repo,user)
        screens=ScreenService(repo,config(),MockPaymentService(),MockProvisioningService())
        for action in (f'sub:{sub.id}',f'keydetail:{key.id}:0'):
            screen=await screens.render(action,user)
            gifts=[(label,target) for row in screen.rows for label,target in row if target.startswith('g:input:subscription:')]
            assert gifts==[('🎁 Подарить другу',f'g:input:subscription:{sub.id}')]
        extra=await screens.render(f'subscription_extra:{sub.id}',user)
        assert not any(target.startswith('g:input:subscription:') for row in extra.rows for _,target in row)
