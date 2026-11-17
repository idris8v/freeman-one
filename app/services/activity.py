"""Operational history without payment requisites or message contents."""
from datetime import timezone,datetime
from sqlalchemy import select, update,cast,String,literal
from app.database.models import Activity, BalanceTransaction, ChatNotice, User, Payment, Subscription, now
from app.services.screens import money

AUDIT_NAMES = {'extend': 'Продление подписки', 'block': 'Блокировка клиента изменена',
    'key': 'Создан ключ', 'keyfreeze': 'Заморозка ключа изменена', 'keydelete': 'Ключ удалён',
    'subfreeze': 'Заморозка подписки изменена', 'key_assign': 'Ключ назначен клиенту',
    'withdrawal_paid': 'Вывод отмечен как выплаченный', 'withdrawal_rejected': 'Заявка на вывод отклонена'}
HISTORY_CATEGORIES = {'clients': ('Регистрации клиентов', '1'), 'sales': ('Подтверждённые покупки и возвраты', '1'),
    'subscriptions': ('Выдачи и продления подписок', '1'), 'trials': ('Пробные активации', '0'),
    'expired': ('Окончания подписок', '0'), 'keys': ('Операции с ключами', '0'),
    'referrals': ('Реферальные начисления', '1'), 'withdrawals': ('Заявки и выплаты', '1'),
    'support': ('События тикетов', '0'), 'errors': ('Неуспешные платежи', '0'), 'gifts': ('Подарки', '0')}


async def infrastructure_target(repo):
    return await repo.setting('infra_chat_id') or await repo.setting('history_chat_id')


async def publish_to_history(repo,event):
    key=event.dedup
    category=None
    if key.startswith('user.registered:'):
        category='clients'
    elif key.startswith('payment:'):
        category='errors' if key.endswith(':failed') else 'sales'
    elif key.startswith('sale.corrected:'):
        category='sales'
    elif key.startswith('infra.alert:'):
        return True
    elif key.startswith('subscription.created:'):
        # A paid purchase has one payment card, not a duplicate creation card.
        paid=await repo.session.scalar(select(Payment.id).where(Payment.subscription_id==int(key.split(':')[1]),Payment.status.in_(('succeeded','order_paid','stars_paid','balance_paid','manual_paid'))).limit(1))
        if paid:return False
        category='trials' if 'Тип: Пробная' in event.text else 'subscriptions'
    elif key.startswith('subscription.expired:'):
        category='expired'
    elif key.startswith('subscription.renewed:'):
        category='subscriptions'
    elif key.startswith('key.'):
        category='keys'
    elif key.startswith('ticket.'):
        category='support'
    elif key.startswith('gift.'):
        category='gifts'
    elif key.startswith('balance.event:'):
        transaction = await repo.session.get(BalanceTransaction, int(key.split(':')[1]))
        if transaction and transaction.type == 'withdrawal_release':
            return False  # The withdrawal audit emits the one rejection card.
        category = 'referrals' if transaction and transaction.type in ('referral_reward', 'referral_reversal') else 'withdrawals'
    elif key.startswith('admin.action:'):
        from app.database.models import AdminAudit
        audit=await repo.session.get(AdminAudit,int(key.split(':')[1]))
        if audit:
            category='clients' if audit.action=='block' else 'subscriptions' if audit.action in ('extend','subfreeze') else 'withdrawals' if audit.action.startswith('withdrawal_') else 'keys'
    return bool(category) and await repo.setting('history_category:'+category,HISTORY_CATEGORIES[category][1])=='1'


async def record(repo, key, text):
    if await repo.session.scalar(select(Activity.id).where(Activity.dedup == key)):
        return
    await repo.add(Activity(dedup=key, text=text[:4096]))


async def baseline_payments(repo):
    # Snapshot current terminal states, not payment creation dates: an older
    # pending order confirmed after binding must still produce a notification.
    statuses=('succeeded','order_paid','stars_paid','balance_paid','manual_paid','refunded','failed')
    rows=await repo.session.scalars(select(Payment).where(Payment.status.in_(statuses),~select(Activity.id).where(Activity.dedup==literal('payment:')+cast(Payment.id,String)+literal(':')+Payment.status).exists()))
    for payment in rows:
        await repo.add(Activity(dedup=f'payment:{payment.id}:{payment.status}',text=f'Исторический платёж #{payment.id} · {payment.status} · {money(payment.amount)}',queued=True))


async def prepare_history(repo):
    # Only confirmed payment statuses produce financial events.
    cutoff=await repo.setting('history_events_after')
    if not cutoff and await repo.setting('history_chat_id'):
        from app.services.admin_notifications import bind
        await bind(repo,int(await repo.setting('history_chat_id')))
        cutoff=await repo.setting('history_events_after')
    payments = await repo.session.scalars(select(Payment).where(Payment.status.in_(['succeeded','order_paid','stars_paid','balance_paid','manual_paid','refunded','failed']),~select(Activity.id).where(Activity.dedup==literal('payment:')+cast(Payment.id,String)+literal(':')+Payment.status).exists()))
    from app.services.public_proxy import queue_purchase_notice
    for payment in payments:
        user = await repo.session.get(User, payment.user_id)
        labels = {'succeeded': '✅ Подтверждена покупка', 'order_paid': '✅ Подтверждена покупка ЮKassa', 'stars_paid': '⭐ Подтверждена покупка Stars', 'balance_paid':'✅ Покупка с баланса', 'manual_paid':'✅ Ручная продажа', 'refunded': '↩️ Возврат платежа', 'failed': '❌ Неуспешный платёж'}
        sub = await repo.session.get(Subscription, payment.subscription_id) if payment.subscription_id else None
        await queue_purchase_notice(repo, user, payment, sub)
        name = await repo.setting(f'subscription_name:{sub.id}', f'#{sub.id}') if sub else 'без привязки'
        await record(repo, f'payment:{payment.id}:{payment.status}',
            f'{labels[payment.status]} #{payment.id}\nКлиент: {user.telegram_id} · {user.display_name}\nПодписка: {name}\nСумма: {money(payment.amount)}')
    target = await repo.setting('history_chat_id')
    infra_target = await infrastructure_target(repo)
    if not target and not infra_target:
        return
    pending = select(Activity).where(Activity.queued == False)
    if not target:
        pending = pending.where(Activity.dedup.startswith('infra.alert:'))
    events = list(await repo.session.scalars(pending.order_by(Activity.id).limit(200)))
    for event in events:
        infra = event.dedup.startswith('infra.alert:')
        destination = infra_target if infra else target
        if not destination:
            continue
        result = await repo.session.execute(update(Activity).where(Activity.id == event.id, Activity.queued == False).values(queued=True))
        if result.rowcount and await publish_to_history(repo,event):
            await repo.add(ChatNotice(dedup=f'activity:{event.id}', target_id=int(destination), kind='infra_alert' if infra else 'history',
                text=f'🗓 {event.created_at:%d.%m.%Y %H:%M} UTC\n{event.text}'))


async def prepare_history_maintenance(repo):
    expired = await repo.session.scalars(select(Subscription).where(Subscription.expires_at <= now()))
    for sub in expired:
        # Only subscriptions tracked since this feature was enabled; no historical flood.
        tracked = await repo.session.scalar(select(Activity.id).where(Activity.dedup == f'subscription.created:{sub.id}'))
        if tracked:
            user = await repo.session.get(User, sub.user_id)
            await record(repo, f'subscription.expired:{sub.id}:{sub.expires_at.replace(tzinfo=timezone.utc).isoformat()}',
                f'⌛ Закончилась подписка #{sub.id}\nКлиент: {user.telegram_id}\nСрок: {sub.expires_at:%d.%m.%Y %H:%M} UTC')
    from app.services.admin_notifications import queue_digest
    await queue_digest(repo)
