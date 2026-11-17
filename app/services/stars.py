"""Telegram Stars checkout for digital subscriptions sold inside Telegram."""
import json
import secrets

from aiogram.types import LabeledPrice
from sqlalchemy import update

from app.database.models import ChatNotice, Payment


# Estimated 2026-11-17 reward: USD 0.013 × RUB 82.5143/USD.
KOPECKS_PER_STAR_SCALED = 10_726_859
KOPECKS_SCALE = 100_000
MAX_INVOICE_STARS = 10_000


def stars_for_kopecks(amount: int) -> int:
    if amount <= 0:
        return 0
    stars = (amount * KOPECKS_SCALE + KOPECKS_PER_STAR_SCALED - 1) // KOPECKS_PER_STAR_SCALED
    return stars if stars <= MAX_INVOICE_STARS else 0


async def configured_stars(repo, tariff, *, addon=False):
    key = 'stars_antig_override' if addon else 'stars_tariff_override'
    value = await repo.tariff_setting(tariff, key, '')
    return int(value) if value.isascii() and value.isdigit() else None


async def star_amount(repo, tariff, amount, *, addon=False):
    override = await configured_stars(repo, tariff, addon=addon)
    if override is None:
        return stars_for_kopecks(amount)
    if addon:
        from app.services.bot_antig_price import tariff_total
        nominal = await tariff_total(repo, tariff, discounted=False)
    else:
        nominal = tariff.price
    if nominal and amount < nominal:
        return (override * amount + nominal - 1) // nominal
    return override


async def bundle_stars(repo, tariff, base, surcharge):
    base_stars = await star_amount(repo, tariff, base)
    extra_stars = await star_amount(repo, tariff, surcharge, addon=True)
    if base > 0 and base_stars == 0:
        return 0
    if surcharge > 0 and extra_stars == 0 and await configured_stars(repo, tariff, addon=True) != 0:
        return 0
    return base_stars + extra_stars


class StarService:
    def __init__(self, repo, settings):
        self.repo, self.settings = repo, settings

    async def enabled(self):
        return await self.repo.setting('payment_provider:stars', '0') == '1'

    async def create(self, user, months, sid=0, *, antig=False):
        if not await self.enabled():
            raise ValueError('Оплата Telegram Stars сейчас выключена.')
        tariff = await self.repo.lock_tariff(await self.repo.tariff(months))
        if not tariff or await self.repo.tariff_setting(tariff, 'tariff_enabled', '1') != '1' or await self.repo.tariff_setting(tariff, 'tariff_deleted', '0') == '1' or user.status == 'blocked':
            raise ValueError('Этот тариф пока нельзя оплатить Telegram Stars.')
        from app.services.billing import BillingService, purchase_limits
        devices,traffic_limit=await purchase_limits(self.repo,tariff)
        if sid:
            await BillingService(self.repo, self.settings).valid(user, sid)
        from app.services.pricing import quote
        from app.services.offers import lock_customer,reserve
        await lock_customer(self.repo,user.id)
        rub_amount = (await quote(self.repo, user.id, tariff,sid))[0]
        base_amount = rub_amount
        access, surcharge = None, 0
        if antig:
            if sid or self.settings.role(user.telegram_id):
                raise ValueError('Этот вариант доступен при покупке новой подписки.')
            from app.services.bot_antig_price import require_enabled, tariff_total
            from app.services.bot_antig_test import resolve
            await require_enabled(self.repo)
            surcharge = await tariff_total(self.repo, tariff)
            stars = await bundle_stars(self.repo, tariff, base_amount, surcharge)
            rub_amount += surcharge
            access = await resolve(self.repo, self.settings, True)
        else:
            stars = await star_amount(self.repo, tariff, base_amount)
        if not 1 <= stars <= MAX_INVOICE_STARS:
            raise ValueError('Этот тариф пока нельзя оплатить Telegram Stars.')
        payment = await self.repo.add(Payment(user_id=user.id, amount=rub_amount, status='stars_created'))
        await reserve(self.repo,payment,tariff,sid,base_amount)
        token = secrets.token_urlsafe(12)
        payload = f'freeman_stars:{payment.id}:{token}'
        await self.repo.set_setting(f'stars_order:{payment.id}', json.dumps({
            'actor': user.id, 'months': tariff.months, 'tariff_id': tariff.id, 'sid': sid, 'rub': rub_amount,
            'stars': stars, 'payload': payload,'devices':devices,'traffic_bytes':traffic_limit,
            'bot_test_access': access, 'antig_surcharge': surcharge,
        }))
        await self.repo.track_funnel(user.id, 'checkout', str(tariff.months))
        return {
            'title': f'F R E E M A N · {tariff.months} мес.',
            'description': (f'Подписка на {tariff.months} мес.' + (' с антиглушилкой.' if antig else '')
                            + ' После оплаты доступ появится автоматически.'),
            'payload': payload,
            'provider_token': '',
            'currency': 'XTR',
            'prices': [LabeledPrice(label=f'Подписка · {tariff.months} мес.', amount=stars)],
        }

    async def _order(self, payload):
        parts = payload.split(':')
        if len(parts) != 3 or parts[0] != 'freeman_stars' or not parts[1].isdigit():
            raise ValueError('Счёт Telegram Stars не распознан.')
        payment = await self.repo.session.get(Payment, int(parts[1]), populate_existing=True)
        if not payment or not payment.status.startswith('stars_'):
            raise ValueError('Счёт Telegram Stars недоступен.')
        try:
            data = json.loads(await self.repo.setting(f'stars_order:{payment.id}', '{}'))
        except json.JSONDecodeError:
            data = {}
        if data.get('payload') != payload or data.get('rub') != payment.amount:
            raise ValueError('Данные счёта Telegram Stars повреждены.')
        return payment, data

    async def precheckout(self, query, user):
        payment, data = await self._order(query.invoice_payload)
        if payment.user_id != user.id or data.get('actor') != user.id:
            raise ValueError('Этот счёт создан для другого пользователя.')
        if query.currency != 'XTR' or query.total_amount != data.get('stars'):
            raise ValueError('Цена счёта изменилась. Выберите тариф заново.')
        if payment.status not in ('stars_created', 'stars_pending'):
            raise ValueError('Этот счёт уже обработан.')
        tariff = await self.repo.select_tariff(data.get('months'),data.get('tariff_id'))
        if not tariff or await self.repo.tariff_setting(tariff, 'tariff_enabled', '1') != '1' or not isinstance(data.get('stars'),int) or not 1<=data['stars']<=10000:
            raise ValueError('Тариф или цена изменились. Выберите тариф заново.')
        if data.get('sid'):
            from app.services.billing import BillingService
            await BillingService(self.repo, self.settings).valid(user, data['sid'])
        if data.get('bot_test_access'):
            from app.services.bot_antig_price import require_enabled
            await require_enabled(self.repo)
        payment.status = 'stars_pending'
        return True

    async def complete(self, successful, user):
        payment, data = await self._order(successful.invoice_payload)
        if payment.user_id != user.id or data.get('actor') != user.id or successful.currency != 'XTR' or successful.total_amount != data.get('stars'):
            raise ValueError('Оплата Telegram Stars не прошла проверку.')
        if payment.status == 'stars_paid':
            return await self.success_screen(payment, user)
        claimed = await self.repo.session.execute(update(Payment).where(
            Payment.id == payment.id, Payment.status.in_(('stars_created', 'stars_pending'))
        ).values(status='stars_processing'))
        if not claimed.rowcount:
            await self.repo.session.refresh(payment)
            if payment.status == 'stars_paid':
                return await self.success_screen(payment, user)
            raise ValueError('Оплата уже обрабатывается. Откройте «Мои подписки» через несколько секунд.')
        tariff = await self.repo.order_tariff(data)
        from app.services.billing import BillingService
        billing = BillingService(self.repo, self.settings)
        sub, key = await billing.valid(user, data['sid']) if data.get('sid') else (None, None)
        sub, key = await billing.fulfill(user, tariff, sub, key,terms=data,operation_key=f"payment:{payment.id}")
        payment.subscription_id = sub.id
        payment.status = 'stars_paid'
        from app.services.offers import consume
        await consume(self.repo,payment)
        from app.web.admin_reports import record_sale
        await record_sale(self.repo,payment,data['months'],sub.device_limit,renewal=bool(data.get('sid')),tariff=tariff,source='telegram')
        if data.get('bot_test_access'):
            snapshot_key = f'sale_snapshot:{payment.id}'
            snapshot = json.loads(await self.repo.setting(snapshot_key))
            snapshot.update(antig_surcharge=data['antig_surcharge'], bot_test_access=data['bot_test_access'])
            await self.repo.set_setting(snapshot_key, json.dumps(snapshot))
        payment.provider_id = 'stars:' + successful.telegram_payment_charge_id
        await self.repo.track_funnel(user.id, 'paid', str(data['months']))
        if user.telegram_id:
            await self.repo.add(ChatNotice(
                dedup=f'stars:{payment.id}', target_id=user.telegram_id, kind='client',
                text=f'✅ Оплата {data["stars"]} ⭐ подтверждена. Подписка #{sub.id} активна.',
                buttons=json.dumps([[['📦 Открыть подписку', f'sub:{sub.id}']]])
            ))
        return await self.success_screen(payment, user)

    async def success_screen(self, payment, user):
        from app.services.paid_subscription import success_screen
        return await success_screen(self.repo, self.settings, payment, user)
