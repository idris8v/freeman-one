"""Remove unused plans; retain referenced and legacy identifiers as archives."""
import json

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.database.models import (
    AppSetting, Gift, PersonalOffer, ProxyAccessDetails, ProxyPlan, Subscription,
    Tariff, VlessRenewal,
)
from app.services.customer_identity import lock_nonce


async def referenced(repo, kind, identifier):
    session = repo.session
    if kind == "vpn":
        for model in (Subscription, Gift):
            if await session.scalar(select(model.id).where(model.tariff_id == identifier).limit(1)):
                return True
    elif await session.scalar(select(ProxyAccessDetails.id).where(ProxyAccessDetails.plan_id == identifier).limit(1)):
        return True
    # Historical and pending orders retain their original snapshots, including
    # orders that are not linked to a Subscription yet.
    prefixes = ("order:", "stars_order:", "sale_snapshot:", "web_checkout:", "balance_purchase:", "admin_grant:", "manual_sale:") if kind == "vpn" else ("proxy_order:", "proxy_sale:")
    field = "tariff_id" if kind == "vpn" else "plan_id"
    values = await session.scalars(select(AppSetting.value).where(or_(*(AppSetting.key.startswith(p) for p in prefixes))))

    def contains(value):
        if isinstance(value, dict):
            return str(value.get(field)) == str(identifier) or (kind == 'vpn' and value.get('months') == f'id{identifier}') or any(contains(v) for v in value.values())
        return isinstance(value, list) and any(contains(v) for v in value)

    for raw in values:
        try:
            if contains(json.loads(raw)):
                return True
        except (TypeError, ValueError):
            return True  # Unknown history cannot prove physical deletion safe.
    if kind == 'vpn':
        bindings = await session.scalars(select(AppSetting.value).where(or_(*(AppSetting.key.startswith(p) for p in ('legacy_tariff:', 'guest_tariff:', 'auto_renew_tariff:')))))
        if str(identifier) in bindings:
            return True
        for raw in await session.scalars(select(VlessRenewal.request)):
            try:
                if contains(json.loads(raw)):
                    return True
            except (TypeError, ValueError):
                return True
        try:
            proxy_rules = json.loads(await repo.setting('proxy_control_settings', '{}'))
            if not isinstance(proxy_rules, dict):
                return True
            if identifier in proxy_rules.get('eligible_tariffs', []):
                return True
        except (TypeError, ValueError):
            return True
    for raw in await session.scalars(select(AppSetting.value).where(AppSetting.key.startswith('public_promotion:'))):
        try:
            rule = json.loads(raw)
            if not isinstance(rule, dict):
                return True
            if rule.get('product') == kind and str(identifier) in map(str, rule.get('plan_ids', [])):
                return True
        except (TypeError, ValueError):
            return True
    for raw in await session.scalars(select(PersonalOffer.tariff_ids).where(PersonalOffer.target.in_(("any", "renewal", "tariffs") if kind == "vpn" else ("proxy",)))):
        try:
            selected = json.loads(raw)
            if not isinstance(selected, list) or str(identifier) in map(str, selected):
                return True
        except (TypeError, ValueError):
            return True
    return False


async def retire(repo, kind, identifier, version):
    await lock_nonce(repo.session, f"catalog:{kind}:{identifier}")
    model = Tariff if kind == "vpn" else ProxyPlan
    item = await repo.session.scalar(select(model).where(model.id == identifier).with_for_update())
    marker = f"catalog_{kind}:{identifier}"
    meta = json.loads(await repo.setting(marker, "{}"))
    if item is None:
        return dict(ok=True, id=identifier, deleted=True, purged=True, archived=True, history_preserved=True) if meta.get('purged') else None
    if meta.get("retired") or meta.get("deleted"):
        return dict(ok=True, id=identifier, deleted=bool(meta.get("deleted")), archived=True, history_preserved=True, reason=meta.get('retirement_reason', 'История и прежние идентификаторы сохранены.'))
    if version != meta.get("version", 0):
        raise ValueError("Тариф изменён. Обновите список и повторите действие")
    used = await referenced(repo, kind, identifier)
    if kind == "vpn":
        await repo.set_tariff_setting(item, "tariff_enabled", "0")
        await repo.set_tariff_setting(item, "tariff_deleted", "1")
    else:
        item.enabled = False
    if not used:
        try:
            async with repo.session.begin_nested():
                await repo.session.delete(item)
                await repo.session.flush()
        except IntegrityError:
            used = True  # A new/future FK is a reason to archive, never a 500.
    reason = 'Сохранён для подписок, заказов, предложений или совместимости прежних тарифов.' if used else ''
    meta.update(deleted=not used, purged=not used, archived=True, retired=True, version=version + 1, retirement_reason=reason)
    await repo.set_setting(marker, json.dumps(meta))
    return dict(ok=True, id=identifier, deleted=not used, purged=not used, archived=True, history_preserved=True, reason=reason)
