"""Full-wallet settlement of an authenticated, frozen widget order.

The caller owns the customer and payment locks. No synthetic provider receipt,
provider API call, administrator grant, or bypass of the frozen access profile.
"""
import json
from app.database.models import BalanceTransaction


async def settle(checkout,payment,user):
    repo=checkout.repo
    if payment.user_id!=user.id or user.status=='blocked':raise PermissionError('Заказ недоступен.')
    if payment.status=='order_paid':return
    if (payment.status!='order_created' or payment.provider_id
        or await repo.setting(f'bot_checkout_started:{payment.id}')):
        raise ValueError('Оплата СБП уже начата. Завершите её и проверьте результат; списание с баланса не выполнено.')
    marker=await repo.setting(f'widget_order:{payment.id}')
    order=await checkout.service.details(payment)
    if not marker or order.get('widget_intent')!=marker or order.get('sid')!=0 or order.get('balance_applied')!=0:
        raise ValueError('Заказ недоступен для оплаты с баланса.')
    if payment.amount!=order.get('total') or payment.amount<100:
        raise ValueError('Некорректная сумма заказа.')
    available=await repo.balance(user.id)
    if available<payment.amount:
        raise ValueError('Недостаточно средств на балансе. Списание не выполнено.')
    # Serialized against /pay/bot before exposing or consuming a bank invoice.
    # The whole debit, fulfilment and settlement commit together. Replays use
    # the same existing payment:<id> provisioning idempotency key.
    payment.status='order_processing'
    order['method']='balance'
    await repo.set_setting(f'order:{payment.id}',json.dumps(order))
    await repo.add(BalanceTransaction(user_id=user.id,type='subscription_purchase',amount=-payment.amount,
        description=f'Оплата подписки из виджета · заказ #{payment.id}'))
    from app.services.billing import BillingService
    tariff=await repo.order_tariff(order)
    sub,key=await BillingService(repo,checkout.settings).fulfill(user,tariff,terms=order,operation_key=f'payment:{payment.id}')
    if key.mode!='vless':raise ValueError('Рабочее подключение не подтверждено. Списание не завершено.')
    payment.subscription_id=sub.id;payment.status='order_paid'
    from app.services.offers import consume
    await consume(repo,payment,order['total'])
    from app.web.admin_reports import record_sale
    await record_sale(repo,payment,order['months'],sub.device_limit,total=order['total'],tariff=tariff,source='telegram')
    await repo.track_funnel(user.id,'paid',str(order['months']))
