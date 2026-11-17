"""Set-based equivalent of the shared active-key rule for web registry scans.

The bot predicate is unchanged. Credentials reference existing subscriptions via
foreign keys; legacy setting links additionally require an existing subscription
and exact canonical subscription_key:<id> / key-id strings, as in that rule.
"""
from sqlalchemy import String, and_, cast, literal, or_, select, union

from app.database.models import (
    AccessKey, AppSetting, Subscription, VlessCredential, VpnCredential, now,
)


def active_key(key=AccessKey):
    sid = cast(Subscription.id, String)
    stopped = select(Subscription.id.label('sid')).join(AppSetting, or_(
        and_(AppSetting.key == literal('subscription_status:') + sid,
             AppSetting.value != 'active'),
        and_(AppSetting.key == literal('subscription_removed:') + sid,
             AppSetting.value == '1'),
        and_(AppSetting.key == literal('subscription_transferred:') + sid,
             AppSetting.value == '1'),
    )).distinct().cte()
    linked = []
    for model in (VlessCredential, VpnCredential):
        linked.append(select(cast(model.access_key_id, String).label('kid')).join(
            stopped, stopped.c.sid == model.subscription_id,
        ).where(model.access_key_id.is_not(None)))
    linked.append(select(AppSetting.value.label('kid')).join(stopped,
        AppSetting.key == literal('subscription_key:') + cast(stopped.c.sid, String),
    ).where(AppSetting.value.is_not(None)))
    stopped_keys = union(*linked).subquery()
    return and_(
        key.status == 'active', key.expires_at > now(),
        cast(key.id, String).not_in(select(stopped_keys.c.kid)),
    )
