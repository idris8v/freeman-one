"""Read-only website presentation of message campaigns, separate from publications."""
from sqlalchemy import select
from app.database.models import PersonalOffer


async def present_notifications(session, user_id, notices):
    """A broadcast is the message itself, never a publication identity.

    Recheck ownership when resolving metadata. Stored notifications and read
    receipts remain unchanged, including historical campaigns.
    """
    ids = {n['offer_id'] for n in notices if n.get('offer_id')}
    messages = set(await session.scalars(select(PersonalOffer.id).where(
        PersonalOffer.id.in_(ids), PersonalOffer.user_id == user_id,
        PersonalOffer.target == 'message',
    ))) if ids else set()
    from app.database.repositories import SQLRepository
    repo = SQLRepository(session)
    proxy_meta = await proxy_notifications(session,user_id)
    result=[]
    for n in notices:
        meta=proxy_meta.get(n.get('offer_id'))
        if meta:
            if not await proxy_notification_current(repo,user_id,meta):continue
            result.append(n | {'kind':'expiry' if meta.get('event')!='activated' else 'payment','target':'proxy'})
        else:
            result.append(n | {'kind':'message','target':''} if n.get('offer_id') in messages else n)
    return result


async def proxy_notifications(session,uid):
    from app.database.models import OfferNotification, AppSetting
    ids=select(OfferNotification.offer_id).where(OfferNotification.user_id==uid).order_by(OfferNotification.id.desc()).limit(50)
    owned=list(await session.scalars(ids));keys=[f'proxy_notice_offer:{x}' for x in owned]
    result={}
    for row in await session.scalars(select(AppSetting).where(AppSetting.key.in_(keys))):
        try:
            data=json.loads(row.value)
            if isinstance(data,dict):result[int(row.key.rsplit(':',1)[1])]=data
        except (ValueError,TypeError):pass
    return result


async def proxy_notification_current(repo,uid,data):
    from app.database.models import ProxyCredential,User,now
    from app.services.proxy_control import config
    from app.services.offers import utc
    from app.services.reminder_policy import stage
    c=await repo.session.get(ProxyCredential,data.get('credential_id'))
    if not c or c.user_id!=uid or c.status in ('deleted','migrated','blocked','missing'):return False
    if c.status=='disabled':
        from app.web.proxy_renewal import can_renew_proxy
        if not await can_renew_proxy(repo,c):return False
    if utc(c.expires_at).isoformat()!=data.get('expiry'):return False
    user=await repo.session.get(User,uid)
    if not user or user.status=='blocked' or await repo.setting(f'notifications:{uid}','1')!='1':return False
    cfg=await config(repo);event=data.get('event')
    if event=='activated':return cfg['notify_activation'] and c.status=='active' and utc(c.expires_at)>now()
    if not cfg['notify_expiry'] or await repo.setting('reminders_enabled','1')!='1':return False
    if event=='expired':return now()-timedelta(days=7)<=utc(c.expires_at)<=now()
    return event in ('expiry:1','expiry:3') and stage(c.expires_at,now())==int(event.split(':')[1])



"""Read-only customer event projection. No delivery jobs or financial mutations."""
import json
from datetime import timedelta, timezone
from sqlalchemy import select
from app.database.models import AppSetting, BalanceTransaction, Payment, Referral, ReferralReward, Subscription, ProxyCredential, Ticket, TicketMessage, User, now
from app.services.accounts import display_freeman_id


def event_time(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def event_money(value):
    return f'{value / 100:,.2f}'.replace(',', ' ').replace('.00', '').replace('.', ',') + ' ₽'


async def customer_events(session, uid):
    """Project existing confirmed records; owner predicates are on every query.

    A stable source id is also the read receipt identity. Polling cannot enqueue
    messages, recalculate rewards, or produce a second financial operation.
    """
    from app.database.repositories import SQLRepository
    repo = SQLRepository(session)
    stamp = now(); cutoff = stamp - timedelta(days=30); rows = []
    def add(identity, title, text, target, kind, created):
        rows.append(dict(event_id=identity, title=title, text=text, target=target,
                         kind=kind, occurred_at=event_time(created).isoformat()))
    registrations = await session.execute(select(Referral, User).join(User, User.id == Referral.invitee_id).where(
        Referral.inviter_id == uid, Referral.created_at >= cutoff).order_by(Referral.id.desc()).limit(50))
    for edge, person in registrations:
        who = f'{person.display_name} · {display_freeman_id(person.freeman_id)}' if person.freeman_id else person.display_name
        add(f'referral-{edge.id}', 'По вашей ссылке зарегистрировались',
            f'{who}. Деньги начисляются после подтверждённой покупки подписки.', 'referrals', 'referral', edge.created_at)
    rewards = await session.execute(select(ReferralReward, User).join(User, User.id == ReferralReward.referred_user_id).where(
        ReferralReward.inviter_id == uid, ReferralReward.amount > 0,
        ReferralReward.status.in_(('credited', 'reversed')), ReferralReward.created_at >= cutoff,
        User.referrer_id == uid).order_by(ReferralReward.id.desc()).limit(50))
    for reward, person in rewards:
        who = f'{person.display_name} · {display_freeman_id(person.freeman_id)}' if person.freeman_id else person.display_name
        add(f'reward-{reward.id}', 'Деньги за приглашение начислены',
            f'{who} купил подписку на {event_money(reward.payment_amount)}. Вам начислено {event_money(reward.amount)} ({reward.percentage}%).',
            'referrals', 'money', reward.created_at)
    transactions = await session.scalars(select(BalanceTransaction).where(BalanceTransaction.user_id == uid,
        BalanceTransaction.created_at >= cutoff, BalanceTransaction.type.in_(('yookassa_topup', 'admin_credit', 'referral_reversal', 'gift_refund')))
        .order_by(BalanceTransaction.id.desc()).limit(50))
    for tx in transactions:
        if tx.type == 'referral_reversal':
            add(f'transaction-{tx.id}', 'Начисление за покупку отменено',
                f'Платёж приглашённого возвращён. Из начисленных денег вычтено {event_money(abs(tx.amount))}.', 'referrals', 'warning', tx.created_at)
        elif tx.amount > 0:
            add(f'transaction-{tx.id}', 'Баланс пополнен' if tx.type != 'gift_refund' else 'Деньги за подарок возвращены',
                f'На баланс зачислено {event_money(tx.amount)}.', 'balance', 'money', tx.created_at)
    payments = await session.scalars(select(Payment).where(Payment.user_id == uid, Payment.created_at >= cutoff,
        Payment.status.in_(('succeeded', 'order_paid', 'stars_paid', 'balance_paid', 'refunded', 'order_canceled', 'card_uncertain', 'proxy_order_paid', 'proxy_order_canceled', 'gift_canceled'))).order_by(Payment.id.desc()).limit(50))
    payments = list(payments)
    attempts = {x.key for x in await session.scalars(select(AppSetting).where(AppSetting.key.in_([
        f'card_attempt:{p.id}' for p in payments if p.status in ('order_canceled', 'card_uncertain')])))}
    metadata_keys = [f'{prefix}:{p.id}' for p in payments for prefix in ('customer_gift_order', 'sale_snapshot', 'card_attempt')]
    metadata = {}
    for row in await session.scalars(select(AppSetting).where(AppSetting.key.in_(metadata_keys))):
        try:
            value = json.loads(row.value)
            if isinstance(value, dict):metadata[row.key] = value
        except (ValueError, TypeError):pass
    for payment in payments:
        if payment.status in ('order_canceled', 'card_uncertain'):
            if f'card_attempt:{payment.id}' not in attempts:continue
            uncertain = payment.status == 'card_uncertain'
            add(f'payment-{payment.id}-{payment.status}', 'Результат автопродления уточняется' if uncertain else 'Автопродление не оплачено',
                f'Подписка #{payment.subscription_id}. '+('Не создавайте повторную оплату, пока результат уточняется. Обратитесь в поддержку.' if uncertain else 'Проверьте карту или выберите другой источник оплаты.'),
                'keys', 'warning', payment.created_at)
            continue
        gift = metadata.get(f'customer_gift_order:{payment.id}', {})
        snapshot = metadata.get(f'sale_snapshot:{payment.id}', {})
        if gift.get('buyer') == uid:
            if payment.status == 'gift_canceled':
                title, text = 'Оплата подарка отменена', 'Подарок не оплачен. Подписка не выдавалась.'
            elif payment.status == 'refunded':
                title, text = 'Деньги за подарок возвращены', f'Возвращено {event_money(payment.amount)}.'
            elif gift.get('ready'):
                title, text = 'Подарок оплачен', f'Оплачено {event_money(payment.amount)}. Приглашение другу доступно в боте, в разделе «Купить подписку → Мои подарки». Срок начнётся после получения подарка.'
            else:continue
            add(f'payment-{payment.id}-'+('refunded' if payment.status == 'refunded' else 'canceled' if payment.status == 'gift_canceled' else 'paid'), title, text, 'balance', 'warning' if payment.status in ('refunded','gift_canceled') else 'payment', payment.created_at)
            continue
        if payment.status.startswith('proxy_order_'):
            canceled = payment.status == 'proxy_order_canceled'
            add(f'payment-{payment.id}-'+('canceled' if canceled else 'paid'),
                'Оплата Telegram Proxy отменена' if canceled else 'Telegram Proxy оплачен',
                'Оплата не завершена. Продление не выполнено.' if canceled else f'Оплачено {event_money(payment.amount)}. Срок и подключение доступны в разделе Telegram Proxy.',
                'proxy', 'warning' if canceled else 'payment', payment.created_at)
            continue
        if payment.status == 'gift_canceled':continue
        if snapshot.get('upgrade') and payment.status != 'refunded':
            add(f'payment-{payment.id}-paid', 'Подписка улучшена', f'Доплата {event_money(payment.amount)} подтверждена. Ключ и срок подписки сохранены.', 'keys', 'payment', payment.created_at)
            continue
        refunded = payment.status == 'refunded'
        add(f'payment-{payment.id}-'+('refunded' if refunded else 'paid'),
            'Платёж возвращён' if refunded else 'Оплата подтверждена',
            f'{"Возврат" if refunded else "Оплачено"}: {event_money(payment.amount)}. '+
            (f'Подписка #{payment.subscription_id}.' if payment.subscription_id else 'Подробности доступны в истории платежей.'),
            'balance' if refunded or not payment.subscription_id else 'keys', 'warning' if refunded else 'payment', payment.created_at)
    replies = await session.execute(select(TicketMessage, Ticket).join(Ticket, Ticket.id == TicketMessage.ticket_id).where(
        Ticket.user_id == uid, TicketMessage.role.in_(('admin', 'system')), TicketMessage.created_at >= cutoff,
        ~TicketMessage.source.startswith('auto:', autoescape=True)).order_by(TicketMessage.id.desc()).limit(30))
    for reply, ticket in replies:
        add(f'support-{reply.id}', 'Статус обращения обновлён' if reply.role == 'system' else 'Вам ответила поддержка',
            f'Обращение #{ticket.id}. '+(reply.text[:300] if reply.text else 'Пришло вложение. Откройте чат, чтобы посмотреть.'),
            'support', 'support', reply.created_at)
        rows[-1]['ticket_id'] = ticket.id
        if reply.role == 'admin':
            rows[-1]['message_preview'] = (reply.text or '').strip()[:300] or 'Вложение'
    # Existing reminder preferences remain authoritative, including email-only accounts.
    preferences = {x.key: x.value for x in await session.scalars(select(AppSetting).where(
        AppSetting.key.in_(('reminders_enabled', 'reminder_trials', f'notifications:{uid}'))))}
    viewer = await session.get(User, uid)
    if viewer and viewer.status != 'blocked' and preferences.get('reminders_enabled', '1') == '1' and preferences.get(f'notifications:{uid}', '1') == '1':
        subscriptions = list(await session.scalars(select(Subscription).where(Subscription.user_id == uid,
            Subscription.expires_at.between(stamp-timedelta(days=7), stamp+timedelta(days=3))).order_by(Subscription.expires_at).limit(50)))
        keys = [f'{prefix}:{sub.id}' for sub in subscriptions for prefix in ('subscription_removed', 'subscription_transferred', 'subscription_status')]
        flags = {x.key:x.value for x in await session.scalars(select(AppSetting).where(AppSetting.key.in_(keys)))} if keys else {}
        for sub in subscriptions:
            if sub.kind == 'trial' and preferences.get('reminder_trials', '0') != '1':continue
            if any(flags.get(f'{prefix}:{sub.id}') == '1' for prefix in ('subscription_removed', 'subscription_transferred')) or flags.get(f'subscription_status:{sub.id}', 'active') != 'active':continue
            insufficient = False
            if event_time(sub.expires_at) <= stamp and await repo.setting(f'auto_renew:{sub.id}','0') == '1' and await repo.setting(f'auto_master:{uid}','1') == '1' and await repo.setting(f'auto_source:{sub.id}','balance') == 'balance':
                from app.services.recurring_cards import unsettled_card
                if not await unsettled_card(repo,sub.id):
                    amount = 500 if await repo.setting(f'auto_renew_cycle:{sub.id}','monthly') == 'daily' and sub.kind in ('paid','admin') else None
                    if amount is None:
                        tariff = await repo.auto_tariff(sub.id)
                        if tariff and await repo.tariff_setting(tariff,'tariff_enabled','1') == '1':
                            from app.services.pricing import quote
                            amount = (await quote(repo,uid,tariff,sub.id))[0]
                    insufficient = amount is not None and await repo.balance(uid) < amount
            if insufficient:
                add(f'autorenew-{sub.id}-{int(event_time(sub.expires_at).timestamp())}', 'Недостаточно денег для автопродления', f'Для продления подписки #{sub.id} требуется {event_money(amount)} на балансе. Списание не выполнено. Пополните баланс или измените источник оплаты.', 'balance', 'warning', sub.expires_at)
                continue
            expired = event_time(sub.expires_at) <= stamp
            expiry = event_time(sub.expires_at)
            from app.services.reminder_policy import stage
            due=stage(expiry,stamp)
            add(f'subscription-{sub.id}-{int(expiry.timestamp())}-'+('expired' if expired else 'soon-1' if due==1 else 'soon'),
                'Срок подписки истёк' if expired else 'Подписка скоро закончится',
                f'Подписка #{sub.id}: срок до {expiry:%d.%m.%Y %H:%M} UTC. '+('Продлите подписку, чтобы восстановить доступ.' if expired else 'Проверьте автопродление или продлите подписку заранее.'),
                'keys', 'expiry', expiry if expired else expiry-timedelta(days=due or 3))
            rows[-1]['sid']=sub.id
        proxies = await session.scalars(select(ProxyCredential).where(ProxyCredential.user_id == uid,
            ProxyCredential.status.in_(('active', 'expired', 'disabled')), ProxyCredential.expires_at.between(stamp-timedelta(days=7),stamp+timedelta(days=3)))
            .order_by(ProxyCredential.expires_at).limit(50))
        from app.services.proxy_control import config as proxy_config
        proxy_settings=await proxy_config(repo)
        current_proxy_notices = [data for data in (await proxy_notifications(session,uid)).values() if await proxy_notification_current(repo,uid,data)]
        for proxy in proxies:
            if not proxy_settings['notify_expiry']:continue
            if any(data.get("credential_id")==proxy.id and data.get("event")!="activated" for data in current_proxy_notices):continue
            if proxy.status == 'disabled':
                from app.web.proxy_renewal import can_renew_proxy
                if not await can_renew_proxy(repo, proxy):continue
            expiry=event_time(proxy.expires_at);expired=expiry<=stamp
            from app.services.reminder_policy import stage
            due=stage(expiry,stamp)
            add(f'proxy-{proxy.id}-{int(expiry.timestamp())}-'+('expired' if expired else 'soon-1' if due==1 else 'soon'),
                'Срок Proxy истёк' if expired else 'Proxy скоро закончится',
                f'Telegram Proxy #{proxy.id}: срок до {expiry:%d.%m.%Y %H:%M} UTC.', 'proxy', 'expiry', expiry if expired else expiry-timedelta(days=due or 3))
    return sorted(rows, key=lambda x: (x['occurred_at'],x['event_id']), reverse=True)[:100]


# Website-only read/archive metadata. Original messages and business records survive.
def notification_state_router(database, settings, current_user):
    from typing import Annotated, Literal
    from fastapi import APIRouter, Cookie, Request, HTTPException
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, ConfigDict, Field, StringConstraints
    from sqlalchemy import update, text, delete
    from app.database.models import AppSetting, OfferNotification, now

    Token = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z]:[A-Za-z0-9_-]{1,90}$")]

    class Action(BaseModel):
        model_config = ConfigDict(extra="forbid")
        action: Literal["read", "clear"]
        ids: list[Token] = Field(min_length=1, max_length=300)

    router = APIRouter()

    async def viewer(token):
        user = await current_user(token)
        if not user:
            raise HTTPException(401, "Требуется вход")
        return user

    async def snapshot(session, uid):
        prefix = f"web_notice:{uid}:"
        rows = await session.scalars(select(AppSetting).where(AppSetting.key.startswith(prefix, autoescape=True)).order_by(AppSetting.value.desc(), AppSetting.key).limit(1000))
        read, hidden = [], []
        for row in rows:
            token = row.key[len(prefix):]
            read.append(token)
            if row.value.endswith("|hidden"):
                hidden.append(token)
        return dict(read=read, hidden=hidden)

    @router.get("/api/web/notifications/events")
    async def events(freeman_session: str | None = Cookie(default=None)):
        user = await viewer(freeman_session)
        async with database.sessions() as session:
            items = await customer_events(session, user.id)
            from app.web.admin_notice_events import admin_events
            items = sorted(items + await admin_events(session,user,settings), key=lambda x:(x["occurred_at"],x["event_id"]), reverse=True)[:100]
        return JSONResponse(dict(items=items), headers={"Cache-Control": "no-store"})

    @router.get("/api/web/notifications/state")
    async def state(freeman_session: str | None = Cookie(default=None)):
        user = await viewer(freeman_session)
        async with database.sessions() as session:
            data = await snapshot(session, user.id)
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @router.post("/api/web/notifications/state")
    async def change(body: Action, request: Request, freeman_session: str | None = Cookie(default=None)):
        if request.headers.get("sec-fetch-site") == "cross-site" or request.headers.get("origin") not in (None, settings.web_base_url.rstrip("/")):
            raise HTTPException(403, "Запрос должен быть отправлен с сайта Freeman ONE")
        user = await viewer(freeman_session)
        tokens = sorted(set(body.ids))
        async with database.sessions.begin() as session:
            if session.bind.dialect.name == "postgresql":
                await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"web_notice:{user.id}:"})
            dialect = session.bind.dialect.name
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            elif dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert
            else:
                raise HTTPException(503, "Сохранение уведомлений временно недоступно")
            value = now().isoformat() + ("|hidden" if body.action == "clear" else "|read")
            statement = insert(AppSetting).values([dict(key=f"web_notice:{user.id}:{token}", value=value) for token in tokens])
            # Monotonic state: an overlapping read never resurrects a cleared item.
            statement = statement.on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": statement.excluded.value}) if body.action == "clear" else statement.on_conflict_do_nothing(index_elements=[AppSetting.key])
            await session.execute(statement)
            offer_ids = [int(token[2:]) for token in tokens if token.startswith("o:") and token[2:].isdigit() and 0 < int(token[2:]) <= 9223372036854775807]
            if offer_ids:
                await session.execute(update(OfferNotification).where(OfferNotification.user_id == user.id, OfferNotification.id.in_(offer_ids), OfferNotification.read_at.is_(None)).values(read_at=now()))
            if settings.can_admin_action(user.telegram_id, "admin:t:home"):
                days = [token[2:] for token in tokens if token.startswith("d:")]
                if days:
                    existing = await session.scalars(select(AppSetting.key).where(AppSetting.key.in_(["admin_digest:report:" + day for day in days])))
                    receipts = [dict(key=f"admin_digest:read:{user.id}:" + key.removeprefix("admin_digest:report:"), value=now().isoformat()) for key in existing]
                    if receipts:
                        await session.execute(insert(AppSetting).values(receipts).on_conflict_do_nothing(index_elements=[AppSetting.key]))
            old = list(await session.scalars(select(AppSetting.key).where(AppSetting.key.startswith(f"web_notice:{user.id}:", autoescape=True)).order_by(AppSetting.value.desc(), AppSetting.key).offset(1000)))
            if old:
                await session.execute(delete(AppSetting).where(AppSetting.key.in_(old)))
            data = await snapshot(session, user.id)
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    return router
