from io import BytesIO
from PIL import Image
import qrcode
import pytest
from test_bot import database as database,register
from test_tickets import config
from app.database.repositories import SQLRepository
from app.database.models import AccessKey
from app.services.domain import SubscriptionService
from app.services.demo_keys import key_screen


async def test_qr_encodes_same_link_and_freezing_blocks_all_formats(database,monkeypatch):
    user=await register(database,2)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        await repo.set_setting(f'demo_trial:{user.id}','pending')
        sub=await SubscriptionService(repo,3).activate_trial(user.id)
        key_id=int(await repo.setting(f'subscription_key:{sub.id}'))
        payloads=[];make=qrcode.make
        def capture(payload):
            payloads.append(payload)
            return make(payload)
        monkeypatch.setattr(qrcode,'make',capture)
        link=await key_screen(repo,config(),user,key_id,sub.id,'link')
        qr=await key_screen(repo,config(),user,key_id,sub.id,'qr')
        assert payloads[0] in link.text
        with Image.open(BytesIO(qr.photo[0])) as image:
            assert image.format=='PNG' and image.width==image.height and image.width>100
        await repo.set_setting(f'subscription_status:{sub.id}','frozen')
        for format in ('link','qr','config'):
            with pytest.raises(ValueError):
                await key_screen(repo,config(),user,key_id,sub.id,format)
