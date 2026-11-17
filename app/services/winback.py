"""One Telegram return offer for former paying VPN customers."""
import secrets
from datetime import datetime, timedelta

from sqlalchemy import String, cast, func, literal, or_, select

from app.database.models import AccessKey, AppSetting, Campaign, Delivery, Payment, Subscription, User, now
from app.services.campaign_buttons import DISCOUNT_DAYS, save
from app.web.posts import store_json


AUDIENCE = 'auto_winback'
START = 'bot_winback_started'
CAMPAIGN = 'bot_winback_campaign'
ENABLED = 'bot_winback_enabled'
INACTIVE_DAYS = 30
DISCOUNT = 20
TEXT = (
    '🎁 <b>20% на возвращение в Freeman ONE</b>\n\n'
    'Если снова нужен VPN, заберите скидку на одну оплату. '
    f'После активации она будет действовать {DISCOUNT_DAYS} дней.'
)
PAID_VPN = ('succeeded', 'order_paid', 'stars_paid', 'balance_paid', 'manual_paid')


def paid_sales():
    return select(Payment.user_id.label('uid'), func.max(Payment.created_at).label('last')).where(
        Payment.subscription_id.is_not(None), Payment.amount > 0, Payment.status.in_(PAID_VPN)
    ).group_by(Payment.user_id).subquery()


def candidate_query(stamp):
    cutoff = stamp - timedelta(days=INACTIVE_DAYS)
    sales = paid_sales()
    access = select(Subscription.user_id.label('uid'), func.max(Subscription.expires_at).label('last')
                    ).group_by(Subscription.user_id).subquery()
    recent_key = select(AccessKey.id).where(
        AccessKey.user_id == User.id, AccessKey.expires_at > cutoff, AccessKey.status == 'active'
    ).exists()
    blocked = select(AppSetting.key).where(or_(
        (AppSetting.key == literal('notifications:') + cast(User.id, String)) & (AppSetting.value == '0'),
        (AppSetting.key == literal('user_archived:') + cast(User.id, String)) & (AppSetting.value == '1'),
        (AppSetting.key == literal('staff_role:') + cast(User.telegram_id, String)) & (AppSetting.value != ''),
    )).exists()
    query = select(User).join(sales, sales.c.uid == User.id).join(access, access.c.uid == User.id).where(
        User.status == 'active', User.telegram_id > 0, sales.c.last <= cutoff,
        access.c.last <= cutoff, ~recent_key, ~blocked,
    )
    return query, sales.c.last, access.c.last


async def eligible(repo, settings, user, stamp=None):
    if not user or settings.role(user.telegram_id) or await repo.setting(ENABLED, '1') != '1':
        return False
    query, _, _ = candidate_query(stamp or now())
    return bool(await repo.session.scalar(query.where(User.id == user.id)))


async def prepare(repo, settings, stamp=None):
    if await repo.setting(ENABLED, '1') != '1':
        return
    stamp = stamp or now()
    started = await repo.setting(START)
    if not started:
        # Start with new eligibility events; do not turn launch into a bulk send.
        await repo.set_setting(START, stamp.isoformat())
        return

    owner_ids = settings.owner_ids()
    if not owner_ids:
        return
    prior = select(Delivery.id).join(Campaign).where(
        Campaign.audience == AUDIENCE, Delivery.user_id == User.id,
    ).exists()
    since = max(datetime.fromisoformat(started), stamp - timedelta(days=2)) - timedelta(days=INACTIVE_DAYS)
    query, purchase, expiry = candidate_query(stamp)
    users = list(await repo.session.scalars(query.where(
        ~prior, User.telegram_id.not_in(owner_ids), or_(purchase >= since, expiry >= since)
    ).order_by(User.id).limit(25)))
    if not users:
        return

    # The same owner row serializes campaign creation and rechecks on retry.
    actor = await repo.session.scalar(select(User.telegram_id).where(
        User.telegram_id.in_(owner_ids), User.status == 'active',
    ).order_by(User.id).with_for_update().limit(1))
    if not actor:
        return
    saved = await repo.session.get(AppSetting, CAMPAIGN, populate_existing=True)
    campaign = await repo.session.get(Campaign, int(saved.value), with_for_update=True) if saved else None
    if not campaign:
        campaign = await repo.add(Campaign(actor_id=actor, audience=AUDIENCE, text=TEXT, status='queued'))
        await repo.set_setting(CAMPAIGN, str(campaign.id))
        await store_json(repo, f'campaign_content:{campaign.id}', {'html': TEXT})
        action = f'campaign_deal:{campaign.id}:{secrets.token_hex(8)}'
        await save(repo, campaign.id, {'columns': 1, 'items': [{
            'label': '🎁 Получить скидку 20%', 'target': action, 'style': 'success',
            'deal': {'kind': 'discount', 'value': DISCOUNT},
        }]})
    for user in users:
        if settings.role(user.telegram_id):
            continue
        dedup = f'campaign:{campaign.id}:{user.id}'
        if not await repo.session.scalar(select(Delivery.id).where(Delivery.dedup == dedup)):
            await repo.add(Delivery(dedup=dedup, user_id=user.id, campaign_id=campaign.id, text=TEXT))
            campaign.status = 'queued'
