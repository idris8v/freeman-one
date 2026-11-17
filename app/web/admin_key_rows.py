"""Request-local reads for one page of the administrator's key registry.

No cross-request cache or external provider requests. Shared availability,
return-offer and addon rules remain the authority for their respective flags.
"""
import json
from collections import defaultdict

from sqlalchemy import select

from app.database.models import (
    AccessKey, AppSetting, Device, RemnawaveServer, Subscription,
    Tariff, VlessCredential, VpnCredential, VpnServer,
)
from app.database.repositories import SQLRepository
from app.services.client_registry import active_key
from app.services.offers import eligible
from app.services.proxies import traffic
from app.services.vless_hwid import ACTIVE, marker_key
from app.web.cabinet.profile import ProfileRepository
from app.web.key_antig import addon_active


async def _credentials(session, model, key_ids):
    result = {}
    duplicates = set()
    for item in await session.scalars(select(model).where(model.access_key_id.in_(key_ids))):
        if item.access_key_id in result:
            duplicates.add(item.access_key_id)
        result.setdefault(item.access_key_id, item)
    # Legacy ambiguous rows retain the exact old lookup instead of imposing a
    # new winner via the batch query's potentially different execution plan.
    for key_id in duplicates:
        result[key_id] = await session.scalar(select(model).where(model.access_key_id == key_id))
    return result


async def key_rows(session, rows):
    rows = list(rows)
    if not rows:
        return []
    key_ids = [key.id for key, _ in rows]
    user_ids = {user.id for _, user in rows}
    owned = list(await session.scalars(select(Subscription).where(Subscription.user_id.in_(user_ids))))
    repo = ProfileRepository(session)
    await repo.prefetch_profile(0, owned)
    names = {key: value for key, value in repo.profile_values.items() if key.startswith('subscription_name:')}
    vpn = await _credentials(session, VpnCredential, key_ids)
    vless = await _credentials(session, VlessCredential, key_ids)
    credential_sub_ids = {(vpn.get(key_id) or vless.get(key_id)).subscription_id
                          for key_id in key_ids if key_id in vpn or key_id in vless}
    subscriptions = {sub.id: sub for sub in owned}
    missing = credential_sub_ids - subscriptions.keys()
    if missing:
        subscriptions.update({sub.id: sub for sub in await session.scalars(
            select(Subscription).where(Subscription.id.in_(missing)))})
    linked = {}
    fallback = {}
    for key, user in rows:
        credential = vpn.get(key.id) or vless.get(key.id)
        sub = subscriptions.get(credential.subscription_id) if credential else None
        if sub is None:
            if user.id not in fallback:
                # Preserve the old per-owner query and its candidate order;
                # execute it once per owner, not once per key. Settings were
                # already fetched, so a missing link no longer causes N*M reads.
                fallback[user.id] = list(await session.scalars(
                    select(Subscription).where(Subscription.user_id == user.id)))
            for candidate in fallback[user.id]:
                if await repo.setting(f'subscription_key:{candidate.id}') == str(key.id):
                    sub = candidate
                    break
        linked[key.id] = sub
    sub_ids = {sub.id for sub in linked.values() if sub is not None}
    tariffs = {t.id: t for t in await session.scalars(select(Tariff).where(
        Tariff.id.in_({sub.tariff_id for sub in linked.values() if sub and sub.tariff_id})))}
    panels = {p.id: p for p in await session.scalars(select(RemnawaveServer).where(
        RemnawaveServer.id.in_({c.server_id for c in vless.values()})))}
    servers = {p.id: p for p in await session.scalars(select(VpnServer).where(
        VpnServer.id.in_({c.server_id for c in vpn.values()})))}
    devices = defaultdict(list)
    for device in await session.execute(select(
        Device.id, Device.subscription_id, Device.status, Device.provisioning_id,
    ).where(Device.subscription_id.in_(sub_ids))):
        devices[device.subscription_id].append(device)
    vless_sub_ids = {linked[key_id].id for key_id in vless if linked[key_id] is not None}
    snapshots = dict((await session.execute(select(AppSetting.key, AppSetting.value).where(
        AppSetting.key.in_([marker_key(sid) for sid in vless_sub_ids])))).all())
    available = set(await session.scalars(select(AccessKey.id).where(
        AccessKey.id.in_(key_ids), active_key())))
    items = []
    for key, user in rows:
        sub = linked[key.id]
        vc, wc = vless.get(key.id), vpn.get(key.id)
        local = devices.get(sub.id, []) if sub else []
        count = sum(d.status in ACTIVE for d in local) if sub else None
        observed_at = None
        if vc:
            if sub:
                raw = snapshots.get(marker_key(sub.id))
                metadata = json.loads(raw) if raw else {}
                # Same HWID contract as shared cache_state: duplicate remote IDs
                # count once; missing/empty IDs are distinct local records.
                count = len({d.provisioning_id or f'local:{d.id}' for d in local if d.status in ACTIVE}) if local or raw else None
                observed_at = metadata.get('last_successful_at')
            else:
                count = None
        elif key.mode != 'mock':
            count = None
        blocked = sum(d.status in ('blocked', 'limit_exceeded') for d in local)
        tariff = tariffs.get(sub.tariff_id) if sub and sub.tariff_id else None
        server = panels.get(vc.server_id) if vc else servers.get(wc.server_id) if wc else None
        # Keep the existing shared business predicates; only page data reads are batched.
        return_customer = bool(sub and await eligible(SQLRepository(session), key))
        items.append(dict(
            antig_active=await addon_active(repo, sub), return_customer=return_customer,
            avatar_url=f'/api/web/admin/users/{user.id}/avatar' if user.telegram_id else None,
            connection_url=vc.subscription_url if vc else None,
            subscription_id=sub.id if sub else None, tariff_id=tariff.id if tariff else None,
            server_name=server.name if server else None, duration_months=tariff.months if tariff else None,
            id=key.id, user_id=user.id, name=names.get(f'subscription_name:{sub.id}', key.name) if sub else key.name,
            registration_source=user.registration_source, owner=user.display_name or user.username or 'Клиент',
            freeman_id=user.freeman_id,
            status=key.status if key.status != 'active' or key.id in available else 'disabled',
            mode=key.mode, created_at=key.created_at.isoformat(), expires_at=key.expires_at.isoformat(),
            device_count=count, device_count_observed_at=observed_at,
            device_count_source='hwid' if vc else 'local' if key.mode == 'mock' else 'unavailable',
            device_limit=sub.device_limit if sub else None, blocked_devices=blocked,
            device_over_limit=bool(sub and (blocked or (sub.device_limit > 0 and (count or 0) > sub.device_limit))),
            traffic=traffic(vc.traffic_used if vc else wc.received + wc.sent if wc else 0),
            server_id=vc.server_id if vc else wc.server_id if wc else None,
        ))
    return items
