from sqlalchemy import select, func

from test_bot import database as database, register
from test_daily_topups import merchant_settings
from app.database.models import Payment, Subscription, AccessKey, BalanceTransaction
from app.database.repositories import SQLRepository
from app.services.topups import SubscriptionCheckoutService


class DirectProvider:
    async def create(self,payment,key,description=None,metadata=None):
        self.data={
            'id':'direct-test-payment','status':'pending','paid':False,'test':True,
            'amount':{'value':f'{payment.amount/100:.2f}','currency':'RUB'},
            'metadata':{'freeman_payment':str(payment.id),'freeman_user':str(payment.user_id),**(metadata or {})},
            'confirmation':{'confirmation_url':'https://yoomoney.ru/direct-test'},
        }
        return self.data

    async def get(self,id): return self.data


async def test_direct_yookassa_purchase_keeps_wallet_and_issues_qr(database):
    user=await register(database,2);settings=merchant_settings();provider=DirectProvider()
    async with database.sessions.begin() as session:
        repo=SQLRepository(session);service=SubscriptionCheckoutService(repo,settings,provider)
        screen=await service.prepare(user,1)
        assert 'Заказ #' in screen.text and any(target.startswith('url:https://') for row in screen.rows for _,target in row)
        payment=await session.scalar(select(Payment))
        provider.data.update(status='succeeded',paid=True)
        result=await service.render(f'order_check:{payment.id}',user)
        assert result.photo and await repo.balance(user.id)==0
        assert payment.status=='order_paid' and payment.subscription_id
        assert await session.scalar(select(func.count()).select_from(Subscription))==1
        assert await session.scalar(select(func.count()).select_from(AccessKey))==1


async def test_canceled_partial_checkout_returns_held_balance_once(database):
    user=await register(database,2);settings=merchant_settings();provider=DirectProvider()
    async with database.sessions.begin() as session:
        repo=SQLRepository(session)
        await repo.add(BalanceTransaction(user_id=user.id,type='credit',amount=30000,description='test'))
        tariff=await repo.tariff(1)
        assert tariff.price>30000
        service=SubscriptionCheckoutService(repo,settings,provider)
        await service.prepare(user,1,balance_applied=30000)
        payment=await session.scalar(select(Payment))
        assert payment.amount==tariff.price-30000
        assert await repo.balance(user.id)==0
        provider.data.update(status='canceled',paid=False)
        result=await service.render(f'order_check:{payment.id}',user)
        assert 'отменён' in result.text.lower()
        assert payment.status=='order_canceled'
        assert await repo.balance(user.id)==30000
        await service.credit(payment,provider.data)
        assert await repo.balance(user.id)==30000
        assert await session.scalar(select(func.count()).select_from(Subscription))==0
