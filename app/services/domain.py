from datetime import timedelta
from sqlalchemy.exc import IntegrityError
from app.database.models import (User, Referral, Subscription, PromoActivation,
    PromoCode, Tariff, WebUsefulLink, WebNews, now)


class ReferralService:
    def __init__(self, repo):
        self.repo = repo

    async def register(self, telegram_user, payload=''):
        if payload.startswith('site_ref_'):
            payload='ref_account_'+payload[9:]
        user = await self.repo.user(telegram_user.id)
        if user:
            user.username = telegram_user.username
            user.first_name = telegram_user.first_name
            from app.services.accounts import ensure_account
            await ensure_account(self.repo,user)
            return user
        inviter = None
        if await self.repo.setting('referral_enabled', '1') == '1' and payload.startswith('ref_') and payload[4:].isdigit():
            inviter_id = int(payload[4:])
            if inviter_id != telegram_user.id and inviter_id < 2**63:
                inviter = await self.repo.user(inviter_id)
        elif await self.repo.setting('referral_enabled','1')=='1' and payload.startswith('ref_account_') and payload[12:].isdigit() and int(payload[12:])<2**63:
            inviter=await self.repo.session.get(User,int(payload[12:]))
            if inviter and (inviter.telegram_id==telegram_user.id or inviter.status!='active'): inviter=None
        user = User(telegram_id=telegram_user.id, username=telegram_user.username,
                    first_name=telegram_user.first_name, referrer_id=inviter.id if inviter else None)
        try:
            async with self.repo.session.begin_nested():
                await self.repo.add(user)
                if inviter:
                    await self.repo.add(Referral(inviter_id=inviter.id, invitee_id=user.id))
        except IntegrityError:
            user = await self.repo.user(telegram_user.id)
            if user is None:
                raise
        return user


class SubscriptionService:
    def __init__(self, repo, device_limit):
        self.repo = repo
        self.device_limit = device_limit

    async def rename(self, user_id, subscription_id, name):
        import unicodedata
        subscription = await self.repo.subscription(user_id, subscription_id)
        if not subscription:
            raise ValueError('Подписка недоступна.')
        name = name.strip()
        if not name or len(name) > 60 or any(unicodedata.category(c).startswith('C') for c in name):
            raise ValueError('Введите название от 1 до 60 символов, без переносов строк.')
        await self.repo.set_setting(f'subscription_name:{subscription.id}', name)
        return subscription

    async def activate_trial(self, user_id, *, hours=None, device_limit=None):
        if await self.repo.setting('trial_enabled', '1') != '1':
            return None
        if not await self.repo.claim_trial(user_id):
            return None
        hours = hours if hours is not None else int(await self.repo.setting('trial_hours', '24'))
        limit = device_limit if device_limit is not None else int(await self.repo.setting('device_limit', str(self.device_limit)))
        subscription = Subscription(user_id=user_id, kind='trial', expires_at=now() + timedelta(hours=hours), device_limit=limit)
        await self.repo.add(subscription)
        # Real provisioning reuses this placeholder in the caller's transaction.
        # Only isolated tests attach a non-working sample configuration.
        from app.database.models import AccessKey
        from app.services.demo_keys import create_demo_file, demo_enabled
        key=await self.repo.add(AccessKey(user_id=user_id,name='Пробная подписка',expires_at=subscription.expires_at))
        await self.repo.set_setting(f'subscription_key:{subscription.id}',str(key.id))
        if demo_enabled(self.repo):await create_demo_file(self.repo,key)
        await self.repo.set_setting(f'demo_trial:{user_id}','used')
        return subscription


class PromoService:
    def __init__(self, repo):
        self.repo = repo

    async def apply(self, user_id, code):
        promo = await self.repo.promo(code.strip().upper())
        from app.services.catalog import promo_valid
        if not promo or not await promo_valid(self.repo,promo) or await self.repo.activation(user_id, promo.id):
            return None
        try:
            async with self.repo.session.begin_nested():
                await self.repo.add(PromoActivation(user_id=user_id, promo_id=promo.id))
        except IntegrityError:
            return None
        return promo

    async def discount(self, user_id):
        from sqlalchemy import select
        promos = await self.repo.session.scalars(select(PromoCode).join(PromoActivation, PromoActivation.promo_id == PromoCode.id).where(PromoActivation.user_id == user_id, PromoCode.effect == 'percent_discount'))
        from app.services.catalog import promo_valid
        values = [p.value for p in promos if await promo_valid(self.repo,p)]
        return max(values, default=0)


async def seed(repo, settings):
    for months in (1, 3, 6, 12):
        tariff = await repo.session.scalar(__import__('sqlalchemy').select(Tariff).where(Tariff.months==months).limit(1))
        price = getattr(settings, f'price_{months}') * 100
        if not tariff:
            tariff=await repo.add(Tariff(months=months, price=price, device_limit=settings.device_limit))
        if not await repo.setting(f'legacy_tariff:{months}'):
            original=await repo.tariff(months)
            if original:await repo.set_setting(f'legacy_tariff:{months}',str(original.id))
    if not await repo.promo('FREEMAN10'):
        await repo.add(PromoCode(code='FREEMAN10', effect='percent_discount', value=10))
    from sqlalchemy import select, func
    if not await repo.session.scalar(select(func.count()).select_from(WebUsefulLink)):
        for label,target,icon,position in (
            ('Инструкции по подключению','instructions','book',10),
            ('База знаний','faq','help',20),
            ('Статус серверов','status','status',30),
        ):
            await repo.add(WebUsefulLink(label=label,target=target,icon=icon,position=position))
    if not await repo.session.scalar(select(func.count()).select_from(WebNews)) and await repo.setting('legacy_publication_migrated')!='1':
        await repo.add(WebNews(title='Личный кабинет F R E E M A N',
            text='Подписки, ключи, полезные материалы и поддержка теперь собраны в одном месте.',
            category='Обновление'))
