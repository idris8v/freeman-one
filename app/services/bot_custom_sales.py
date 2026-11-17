"""Explicit custom bot sales, compatible with existing reporting snapshots."""
import json
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import select
from app.database.models import Payment, AdminAudit, now

MONTHLY_DEVICE_PRICE = 15000  # Kopecks; owner's agreed custom-sale base.


def quote(devices, *, hours=None, months=None, label='',explicit_price=None):
    if type(devices) is not int or not 1 <= devices <= 100:
        raise ValueError('Устройства: от 1 до 100.')
    if months is not None:
        if type(months) is not int or not 1 <= months <= 120:
            raise ValueError('Месяцы: от 1 до 120.')
        normalized_hours = months*720
        equivalents = Decimal(months)
    else:
        if type(hours) is not int or not 1 <= hours <= 87600:
            raise ValueError('Срок: от 1 часа до 3650 дней.')
        normalized_hours = hours
        equivalents = Decimal(hours)/720
    total = int((MONTHLY_DEVICE_PRICE*devices*equivalents).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if explicit_price is not None:
        if type(explicit_price) is not int:raise ValueError('Неверная цена.')
        total=explicit_price
    if not 1 <= total <= 100_000_000:
        raise ValueError('Сумма ручной продажи должна быть от 0,01 до 1 000 000 ₽.')
    result=dict(version=1, total=total, rate=MONTHLY_DEVICE_PRICE, devices=devices,
        hours=normalized_hours, months=months if months is not None else hours//720 if hours%720 == 0 else 0,
        term_unit='months' if months is not None else 'hours', term_amount=months if months is not None else hours,
        label=label, tariff_name='Нестандартная подписка · '+label,
        basis='150 RUB/device/month; 30 days per calculation month')
    if explicit_price is not None:result.update(explicit_price=explicit_price,basis='explicit manual price')
    return result


async def record(repo, actor, user, sub, credential, token, terms):
    """Called under the grant confirmation lock, after confirmed provisioning."""
    expected = quote(terms['devices'],
        months=terms['term_amount'] if terms['term_unit']=='months' else None,
        hours=terms['term_amount'] if terms['term_unit']=='hours' else None, label=terms['label'],explicit_price=terms.get('explicit_price'))
    if terms != expected or sub.user_id!=user.id or credential.user_id!=user.id or credential.subscription_id!=sub.id:
        raise ValueError('Условия ручной продажи изменились. Продажа не учтена.')
    reference='bot-custom-sale:'+token
    existing=await repo.session.scalar(select(Payment).where(Payment.provider_id==reference).with_for_update())
    if existing:
        if (existing.user_id,existing.subscription_id,existing.amount,existing.status)!=(user.id,sub.id,terms['total'],'manual_paid'):
            raise ValueError('Эта продажа уже записана с другими условиями.')
        return existing
    payment=await repo.add(Payment(user_id=user.id,subscription_id=sub.id,amount=terms['total'],
        status='manual_paid',provider_id=reference))
    stamp=now().isoformat()
    snapshot=dict(kind='vpn',source='manual',issuance_kind='manual_sale',custom=True,
        sale_category='custom_subscription',tariff_id=None,tariff_name=terms['tariff_name'],
        months=terms['months'],days=str(Decimal(terms['hours'])/24),devices=terms['devices'],
        total=terms['total'],base_price=terms['total'],renewal=False,
        expires_at=sub.expires_at.isoformat(),
        recorded_by=actor.id,key_id=credential.access_key_id,subscription_id=sub.id,
        paid_at=stamp,issued_at=stamp,received_at=stamp,custom_terms=terms)
    await repo.set_setting(f'sale_snapshot:{payment.id}',json.dumps(snapshot))
    await repo.set_setting(f'sale_source:{payment.id}','manual')
    await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='bot_custom_sale',target=str(payment.id),
        details=f'Custom subscription; key={credential.access_key_id}; devices={terms["devices"]}; term={terms["label"]}; kopecks={terms["total"]}'))
    # This is external manual revenue, never a wallet top-up or a provider charge.
    return payment
