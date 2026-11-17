"""Read-only purchase and connection metadata shared by the web key list."""
from datetime import timezone
from sqlalchemy import select, func
from app.database.models import Payment, Device, Tariff, now


PAID_STATUSES = ('succeeded', 'order_paid', 'balance_paid', 'stars_paid')


def iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


async def key_metadata(repo, user_id, keys, subscriptions):
    linked = {}
    for subscription in subscriptions:
        key_id = await repo.setting(f'subscription_key:{subscription.id}')
        if key_id:
            linked[int(key_id)] = subscription
    ids = [sub.id for sub in subscriptions]
    purchases = dict((await repo.session.execute(select(
        Payment.subscription_id, func.min(Payment.created_at)
    ).where(Payment.user_id == user_id, Payment.subscription_id.in_(ids),
            Payment.status.in_(PAID_STATUSES)).group_by(Payment.subscription_id))).all())
    connections = dict((await repo.session.execute(select(
        Device.subscription_id, func.min(Device.created_at)
    ).where(Device.subscription_id.in_(ids), Device.status.in_(('active', 'connected', 'online')))
      .group_by(Device.subscription_id))).all())
    tariffs = {item.id: item for item in await repo.session.scalars(select(Tariff))}
    result = {}
    for key in keys:
        sub = linked.get(key.id)
        purchased = purchases.get(sub.id) if sub else None
        age = (now() - purchased.replace(tzinfo=timezone.utc)).total_seconds() if purchased else None
        result[key.id] = {
            'created_at': iso(key.created_at),
            'purchased_at': iso(purchased),
            # Device creation is registration, not observed network activity.
            'first_device_added_at': iso(connections.get(sub.id)) if sub else None,
            'first_connected_at': None,
            'tariff_id': sub.tariff_id if sub else None,
            'duration_months': tariffs[sub.tariff_id].months if sub and sub.tariff_id in tariffs else None,
            'recently_purchased': age is not None and 0 <= age < 72 * 60 * 60,
        }
    return result
