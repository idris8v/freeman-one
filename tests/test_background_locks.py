"""PostgreSQL concurrency: slow VPN work must not lock unrelated customers."""
import asyncio
import os
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import event, func, select, text, update

from app.config.settings import Settings
from app.database.models import AccessKey, BalanceTransaction, Payment, Subscription, User, now
from app.database.repositories import SQLRepository
from app.services.billing import prepare_autorenew
from app.services.dispatch import DispatchWorker
from app.services.referral_accounts import prepare_rewards
from test_freeman_ai import Tests as Fixture


class BackgroundLocks(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = Fixture.asyncSetUp
    asyncTearDown = Fixture.asyncTearDown

    @unittest.skipUnless(os.environ.get('AI_TEST_POSTGRES') == '1', 'requires PostgreSQL row locks')
    async def test_slow_telegram_start_does_not_block_another_customer(self):
        from aiogram import Bot, Dispatcher
        from aiogram.client.session.base import BaseSession
        from aiogram.types import Chat, Message, Update, User as TelegramUser
        from app.bot.handlers import create_router
        from app.bot.middlewares import ServicesMiddleware
        from app.services.integrations import MockPaymentService, MockProvisioningService

        entered, release = asyncio.Event(), asyncio.Event()

        class TelegramSession(BaseSession):
            def __init__(self):
                super().__init__()
                self.delivered = []

            async def close(self):
                pass

            async def stream_content(self, *args, **kwargs):
                yield b''

            async def make_request(self, bot, method, timeout=None):
                if method.__api_method__ in ('sendMessage', 'sendRichMessage'):
                    if method.chat_id == 123:
                        entered.set()
                        await release.wait()
                    self.delivered.append(method.chat_id)
                    return Message(message_id=10, date=now(), chat=Chat(id=method.chat_id, type='private'))
                return True

        fake = TelegramSession()
        bot = Bot('123456:TEST_TOKEN_NOT_REAL', session=fake)
        dispatcher, router = Dispatcher(), create_router()
        settings = Settings(_env_file=None, encryption_key=self.settings.encryption_key)
        router.message.outer_middleware(ServicesMiddleware(self.db, settings, MockPaymentService(), MockProvisioningService()))
        dispatcher.include_router(router)

        def command(tid):
            person = TelegramUser(id=tid, first_name='Test', is_bot=False)
            return Update(update_id=tid, message=Message(message_id=1, date=now(), chat=Chat(id=tid, type='private'), from_user=person, text='/start'))

        first = asyncio.create_task(dispatcher.feed_update(bot, command(123)))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.wait_for(dispatcher.feed_update(bot, command(456)), 5)
            self.assertIn(456, fake.delivered)
            self.assertNotIn(123, fake.delivered)
        finally:
            release.set()
            await first
            await dispatcher.storage.close()
            await bot.session.close()

    async def daily_subscription(self):
        async with self.db.sessions.begin() as session:
            repo = SQLRepository(session)
            sub = Subscription(user_id=self.uid, kind='paid', expires_at=now()-timedelta(hours=1))
            session.add(sub)
            await session.flush()
            key = AccessKey(user_id=self.uid, name='Test', mode='wireguard', expires_at=sub.expires_at)
            session.add(key)
            session.add(BalanceTransaction(user_id=self.uid, type='test', amount=2000, description='Fixture'))
            await session.flush()
            await repo.set_setting(f'subscription_key:{sub.id}', str(key.id))
            await repo.set_setting(f'auto_renew:{sub.id}', '1')
            await repo.set_setting(f'auto_renew_cycle:{sub.id}', 'daily')
            await repo.set_setting('reminders_enabled', '0')
            return sub.id

    @unittest.skipUnless(os.environ.get('AI_TEST_POSTGRES') == '1', 'requires PostgreSQL row locks')
    async def test_slow_vpn_and_concurrent_renewal(self):
        sid = await self.daily_subscription()
        started, release, contender = asyncio.Event(), asyncio.Event(), asyncio.Event()
        settings = Settings(_env_file=None, encryption_key=self.settings.encryption_key)

        async def vpn_call(*args, **kwargs):
            started.set()
            await release.wait()

        async def another_renewal():
            async with self.db.sessions.begin() as session:
                await prepare_autorenew(SQLRepository(session), settings)

        with patch('app.services.topups.reconcile', new=AsyncMock()), \
             patch('app.services.recurring_cards.prepare_cards', new=AsyncMock()), \
             patch('app.services.vpn.VpnService.credential', new=AsyncMock(return_value=None)), \
             patch('app.services.vpn.VpnService.set_subscription_status', side_effect=vpn_call):
            first = asyncio.create_task(DispatchWorker(self.db, None, settings).prepare())
            second = None
            def before_execute(conn, cursor, statement, parameters, context, executemany):
                if asyncio.current_task() is second and statement.startswith('UPDATE users '):
                    contender.set()
            event.listen(self.db.engine.sync_engine, 'before_cursor_execute', before_execute)
            try:
                await asyncio.wait_for(started.wait(), 5)
                # A real DB lock timeout catches the former all-customer UPDATE.
                async with self.db.sessions.begin() as session:
                    await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
                    await session.execute(update(User).where(User.id == self.other).values(first_name='Still responsive'))
                second = asyncio.create_task(another_renewal())
                await asyncio.wait_for(contender.wait(), 5)
                release.set()
                await asyncio.wait_for(asyncio.gather(first, second), 10)
            finally:
                event.remove(self.db.engine.sync_engine, 'before_cursor_execute', before_execute)
                release.set()
                for task in (first, second):
                    if task and not task.done():
                        task.cancel()
                await asyncio.gather(*(t for t in (first, second) if t), return_exceptions=True)
        async with self.db.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(Payment).where(Payment.subscription_id == sid)), 1)
            self.assertEqual(await SQLRepository(session).balance(self.uid), 1500)

    async def test_reward_sweep_and_refund_remain_idempotent(self):
        from app.database.models import ReferralReward
        async with self.db.sessions.begin() as session:
            (await session.get(User, self.uid)).referrer_id = self.other
            payment = Payment(user_id=self.uid, amount=10000, status='balance_paid', provider_id='fixture-reward')
            session.add(payment)
            await session.flush()
            pid = payment.id
            await prepare_rewards(SQLRepository(session))
            await prepare_rewards(SQLRepository(session))
            reward = await session.scalar(select(ReferralReward).where(ReferralReward.payment_id == pid))
            amount = reward.amount
            self.assertGreater(amount, 0)
            self.assertEqual(await SQLRepository(session).balance(self.other), amount)
            payment.status = 'refunded'
            await session.flush()
            await prepare_rewards(SQLRepository(session))
            await prepare_rewards(SQLRepository(session))
            self.assertEqual(await SQLRepository(session).balance(self.other), 0)
            self.assertEqual(reward.status, 'reversed')
            self.assertEqual(await session.scalar(select(func.count()).select_from(BalanceTransaction).where(BalanceTransaction.user_id == self.other)), 2)
