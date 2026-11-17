"""Verify provider confirmation independently of a temporarily unavailable provisioner."""
from decimal import Decimal, InvalidOperation
async def subscription_confirmed(payment, data, service, settings):
    order = await service.details(payment)
    expected = {'freeman_payment': str(payment.id), 'freeman_user': str(payment.user_id), 'freeman_kind': 'subscription', 'freeman_months': str(order['months']), 'freeman_sid': str(order['sid'])}
    amount = data.get('amount', {})
    try:
        return (data.get('id') == payment.provider_id and data.get('status') == 'succeeded' and data.get('paid') is True and amount.get('currency') == 'RUB' and Decimal(amount.get('value', '0')) * 100 == payment.amount and all(data.get('metadata', {}).get(k) == v for k, v in expected.items()) and bool(data.get('test', False)) == settings.yookassa_test_mode)
    except (InvalidOperation, TypeError):
        return False
