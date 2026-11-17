from urllib.parse import urlparse,parse_qs
import asyncio
import pytest
from sqlalchemy import select,func
from aiogram import Bot,Dispatcher
from aiogram.types import Message,Chat,Update,CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage
from test_bot import database as database,register,person,FakeSession
from test_tickets import config
from app.database.repositories import SQLRepository
from app.database.models import Gift,ChatNotice,AccessKey,now
from app.services.domain import SubscriptionService
from app.services.gifts import GiftService,cancel_legacy_notifications
from app.bot.handlers import create_router
from app.bot.middlewares import ServicesMiddleware
from app.services.integrations import MockPaymentService,MockProvisioningService


async def test_gift_buttons_are_inline_and_manual_input_binds_recipient(database):
    user=await register(database,2)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);await repo.set_setting(f'demo_trial:{user.id}','pending')
        sub=await SubscriptionService(repo,1).activate_trial(user.id)
    fake=FakeSession();bot=Bot('123456:TEST_TOKEN_NOT_REAL',session=fake)
    dispatcher=Dispatcher(storage=MemoryStorage());router=create_router()
    middleware=ServicesMiddleware(database,config(),MockPaymentService(),MockProvisioningService())
    router.message.outer_middleware(middleware);router.callback_query.outer_middleware(middleware)
    dispatcher.include_router(router)
    message=Message(message_id=1,date=now(),chat=Chat(id=2,type='private'),from_user=person(2),text='Choose')
    await dispatcher.feed_update(bot,Update(update_id=1,callback_query=CallbackQuery(id='1',from_user=person(2),chat_instance='test',data=f'g:input:subscription:{sub.id}',message=message)))
    assert not any(getattr(call,'reply_markup',None).__class__.__name__=='ReplyKeyboardMarkup' for call in fake.calls)
    inline=next(call.reply_markup for call in fake.calls if getattr(call,'reply_markup',None).__class__.__name__=='InlineKeyboardMarkup')
    assert inline.inline_keyboard[0][0].url.startswith('https://t.me/share/url?url=')
    payload=parse_qs(urlparse(inline.inline_keyboard[0][0].url).query)
    assert 'Вам подарок' in payload['text'][0] and '«Старт»' in payload['text'][0]
    assert inline.inline_keyboard[-1][0].callback_data==f'sub:{sub.id}'
    action=inline.inline_keyboard[1][0].callback_data
    await dispatcher.feed_update(bot,Update(update_id=2,callback_query=CallbackQuery(id='2',from_user=person(2),chat_instance='test',data=action,message=message)))
    typed=Message(message_id=2,date=now(),chat=Chat(id=2,type='private'),from_user=person(2),text='999')
    await dispatcher.feed_update(bot,Update(update_id=3,message=typed))
    async with database.sessions() as session:
        gift=await session.scalar(select(Gift).where(Gift.recipient_id==999))
        assert gift and gift.status=='draft'
    assert not any('Что-то пошло не так' in (getattr(call,'text','') or '') for call in fake.calls)
    await dispatcher.storage.close();await bot.session.close()


async def test_shared_invitation_claims_immediately_once_without_notifications(database):
    sender=await register(database,2);friend=await register(database,3);stranger=await register(database,4)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);await repo.set_setting(f'demo_trial:{sender.id}','pending')
        sub=await SubscriptionService(repo,1).activate_trial(sender.id)
        key_id=int(await repo.setting(f'subscription_key:{sub.id}'))
        service=GiftService(repo,config())
        screen=await service.render(f'g:input:subscription:{sub.id}',sender)
        await service.render(f'g:input:subscription:{sub.id}',sender)
        assert await session.scalar(select(func.count()).select_from(Gift))==1
        item=await session.scalar(select(Gift))
        clean=parse_qs(urlparse(screen.rows[0][0][1][4:]).query)['url'][0]
        assert clean==service.link(item) and '%' not in clean and ' ' not in clean
        with pytest.raises(ValueError):
            await service.confirm(item,sender)
        received=await service.claim(item.code,friend)
        repeated=await service.claim(item.code,friend)
        assert 'Подарок от' in received.text and received.photo
        assert 'уже получен' in repeated.text and not repeated.photo
        assert await session.scalar(select(func.count()).select_from(ChatNotice))==0
        assert item.status=='claimed' and item.recipient_id==friend.telegram_id
        with pytest.raises(ValueError):
            await service.claim(item.code,stranger)
        assert (await session.get(AccessKey,key_id)).status=='deleted'


async def test_two_claimants_cannot_get_same_open_gift(database):
    sender=await register(database,2);friend=await register(database,3);other=await register(database,4)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);await repo.set_setting(f'demo_trial:{sender.id}','pending')
        sub=await SubscriptionService(repo,1).activate_trial(sender.id)
        await GiftService(repo,config()).render(f'g:input:subscription:{sub.id}',sender)
        gift=await session.scalar(select(Gift));code=gift.code
    async def receive(user):
        async with database.sessions.begin() as session:
            try:
                await GiftService(SQLRepository(session),config()).claim(code,user)
                return True
            except ValueError:
                return False
    assert sum(await asyncio.gather(receive(friend),receive(other)))==1
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AccessKey).where(AccessKey.status=='active'))==1
        assert await session.scalar(select(func.count()).select_from(ChatNotice))==0




async def test_failed_redemption_keeps_invitation_unassigned(database):
    sender=await register(database,2);friend=await register(database,3)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);await repo.set_setting(f'demo_trial:{sender.id}','pending')
        sub=await SubscriptionService(repo,1).activate_trial(sender.id)
        service=GiftService(repo,config())
        await service.render(f'g:input:subscription:{sub.id}',sender)
        gift=await session.scalar(select(Gift))
        key=await session.get(AccessKey,int(await repo.setting(f'subscription_key:{sub.id}')))
        key.status='frozen';await session.flush()
        with pytest.raises(ValueError):
            await service.claim(gift.code,friend)
        await session.refresh(gift)
        assert gift.recipient_id==0 and gift.status=='draft' and not gift.subscription_id
        assert await repo.balance(sender.id)==0


async def test_old_gift_queue_cancelled_without_affecting_other_notices(database):
    async with database.sessions.begin() as session:
        for dedup,status in [('gift.received:1','pending'),('gift_request:1:3','pending'),('gift.approved:1','sending'),('gift.received:2','sent'),('other:1','pending')]:
            session.add(ChatNotice(dedup=dedup,target_id=3,kind='client',text='test',status=status))
        await session.flush();await cancel_legacy_notifications(session)
        statuses=dict((n.dedup,n.status) for n in await session.scalars(select(ChatNotice)))
        assert statuses['gift.received:1']==statuses['gift_request:1:3']==statuses['gift.approved:1']=='cancelled'
        assert statuses['gift.received:2']=='sent' and statuses['other:1']=='pending'


async def test_replayed_gift_start_does_not_send_card_twice(database):
    sender=await register(database,2);friend=await register(database,3)
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);await repo.set_setting(f'demo_trial:{sender.id}','pending')
        sub=await SubscriptionService(repo,1).activate_trial(sender.id)
        await GiftService(repo,config()).render(f'g:input:subscription:{sub.id}',sender)
        gift=await session.scalar(select(Gift));code=gift.code
    fake=FakeSession();bot=Bot('123456:TEST_TOKEN_NOT_REAL',session=fake)
    dispatcher=Dispatcher(storage=MemoryStorage());router=create_router()
    router.message.outer_middleware(ServicesMiddleware(database,config(),MockPaymentService(),MockProvisioningService()))
    dispatcher.include_router(router)
    message=Message(message_id=70,date=now(),chat=Chat(id=3,type='private'),from_user=person(3),text=f'/start gift_{code}')
    await dispatcher.feed_update(bot,Update(update_id=10,message=message))
    count=len(fake.calls)
    await dispatcher.feed_update(bot,Update(update_id=11,message=message))
    assert len(fake.calls)==count
    assert sum(call.__api_method__=='sendRichMessage' for call in fake.calls)==1
    assert not any('Что-то пошло не так' in (getattr(call,'text','') or '') for call in fake.calls)
    await dispatcher.storage.close();await bot.session.close()
