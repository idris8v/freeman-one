"""Website adapter for the bot's frozen Antig invoice contract.

No panel/database schema fork: pricing, squad resolution, paid-profile verification,
provider reconciliation and provisioning remain owned by the shared services.
Only new purchases may select the option. An invoice retains its frozen terms.
"""
import json
import secrets
from app.services import bot_antig_price


async def options(repo, tariffs):
    active = await bot_antig_price.enabled(repo)
    return {'enabled': active, 'prices': {
        str(t.id): await bot_antig_price.tariff_total(repo, t) for t in tariffs
    }}


async def prepare(repo, settings, tariff, base, selected, expected):
    if not selected:
        return None
    await bot_antig_price.require_enabled(repo)
    extra = await bot_antig_price.tariff_total(repo, tariff)
    total = base + extra
    if type(expected) is not int or expected != total:
        raise ValueError('Стоимость изменилась. Обновите тарифы и подтвердите новую сумму.')
    if not 100 <= total <= 100000000:
        raise ValueError('Сумма оплаты должна быть от 1 до 1 000 000 ₽.')
    from app.services.bot_antig_test import resolve
    profile = await resolve(repo, settings, True)
    return dict(base=base, extra=extra, total=total, profile=profile)


async def attach(repo, payment, user, prepared):
    """Bind a server-resolved profile to the same immutable proof the bot uses."""
    order = json.loads(await repo.setting(f'order:{payment.id}', '{}'))
    if (order.get('sid') != 0 or order.get('balance_applied') != 0
            or payment.status != 'order_created' or payment.provider_id):
        raise ValueError('Антиглушилка доступна при покупке новой подписки без частичного списания.')
    if order.get('total') != prepared['base']:
        raise ValueError('Стоимость изменилась. Обновите тарифы перед оплатой.')
    from app.services.bot_widget_checkout import key
    marker = key(secrets.token_hex(16))
    order.update(amount=prepared['total'], total=prepared['total'],
                 bot_test_access=prepared['profile'], antig_surcharge=prepared['extra'],
                 antig_base_total=prepared['base'], widget_intent=marker)
    payment.amount = prepared['total']
    await repo.set_setting(marker, json.dumps(dict(actor=user.id, payment=payment.id,
        source='website', choice=[order['tariff_id'], 1])))
    await repo.set_setting(f'widget_order:{payment.id}', marker)
    await repo.set_setting(f'order:{payment.id}', json.dumps(order))
    await repo.set_setting(f'sale_source:{payment.id}', 'website')


async def create_order(repo, settings, user, tariff, prepared, *, method, return_url):
    """Stage only; the caller commits before any bank HTTP request.

    The existing checkout cannot price an add-on (and rejects a zero base).
    Freeze the same fields as bot_widget_checkout, using its pricing, allowance,
    reservation and paid-profile contracts rather than a synthetic provider event.
    """
    import uuid
    from app.database.models import Payment
    from app.services.billing import purchase_limits
    from app.services.offers import lock_customer, reserve
    from app.services.pricing import quote
    if method not in ('balance', 'yookassa'):
        raise ValueError('Некорректный способ оплаты.')
    if method == 'yookassa' and (not settings.yookassa_shop_id or not settings.yookassa_secret_key
            or await repo.setting('payment_provider:yookassa','1') != '1'):
        raise ValueError('Оплата через ЮKassa сейчас недоступна.')
    tariff = await repo.lock_tariff(tariff)
    if not tariff or user.status != 'active' or await repo.tariff_setting(tariff,'tariff_enabled','1') != '1':
        raise ValueError('Тариф недоступен.')
    devices, traffic = await purchase_limits(repo, tariff)
    await lock_customer(repo, user.id)
    if (await quote(repo,user.id,tariff,0))[0] != prepared['base']:
        raise ValueError('Стоимость изменилась. Обновите тарифы перед оплатой.')
    payment = await repo.add(Payment(user_id=user.id, amount=prepared['base'], status='order_created'))
    await reserve(repo,payment,tariff,0,prepared['base'])
    await repo.set_setting(f'order:{payment.id}',json.dumps(dict(months=tariff.months,
        tariff_id=tariff.id,sid=0,amount=prepared['base'],total=prepared['base'],balance_applied=0,
        payment_method=None,return_url=return_url,devices=devices,traffic_bytes=traffic)))
    await repo.set_setting(f'order_idem:{payment.id}',str(uuid.uuid4()))
    await attach(repo,payment,user,prepared)
    return payment


async def settle_balance(repo, settings, payment, user):
    """Wallet settlement of a frozen website invoice under the customer lock.

    Same proof and payment:<id> provisioning key as provider settlement. The
    caller commits debit + fulfilment + result atomically; never fake a receipt.
    """
    from app.services.topups import SubscriptionCheckoutService
    from app.services.billing import BillingService
    from app.database.models import BalanceTransaction
    service = SubscriptionCheckoutService(repo, settings)
    if payment.user_id != user.id or user.status != 'active':
        raise ValueError('Заказ недоступен.')
    if payment.status != 'order_created' or payment.provider_id:
        raise ValueError('Платёж уже обрабатывается. Проверьте его статус.')
    order = await service.details(payment)
    if (not order.get('bot_test_access') or order.get('sid') != 0
            or order.get('balance_applied') != 0 or payment.amount != order['total']):
        raise ValueError('Параметры заказа не подтверждены.')
    if await repo.balance(user.id) < payment.amount:
        raise ValueError('Недостаточно средств на балансе. Списание не выполнено.')
    payment.status = 'order_processing'
    order['method'] = 'balance'
    await repo.set_setting(f'order:{payment.id}',json.dumps(order))
    await repo.add(BalanceTransaction(user_id=user.id, type='subscription_purchase',
        amount=-payment.amount, description=f'Оплата подписки · заказ #{payment.id}'))
    tariff = await repo.order_tariff(order)
    sub, key = await BillingService(repo, settings).fulfill(user, tariff, terms=order,
                                                          operation_key=f'payment:{payment.id}')
    if key.mode != 'vless':
        raise ValueError('Рабочее подключение не подтверждено. Списание не завершено.')
    payment.subscription_id = sub.id
    payment.status = 'balance_paid'
    from app.services.offers import consume
    await consume(repo, payment, order['total'])
    from app.web.admin_reports import record_sale
    await record_sale(repo, payment, order['months'], sub.device_limit,
                      total=order['total'], tariff=tariff, source='website')
    await repo.track_funnel(user.id, 'paid', str(order['months']))
    return sub, key
