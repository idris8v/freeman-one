from app.services.accounts import display_freeman_id
"""Customer referral summary and reward history chart."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, HTTPException
from sqlalchemy import func, select

from app.database.models import Payment, Referral, ReferralReward, User
from app.database.repositories import SQLRepository
from app.web.cabinet.context import WebContext


def customer_history(invited, people, payments, rewards, payment_amounts):
    """Read-only event projection. Scope is the caller's actual referral edges."""
    allowed = {edge.invitee_id for edge in invited}
    paid = {payment.user_id for payment in payments}
    by_payment = {reward.payment_id: reward for reward in rewards}
    rows = []
    def identity(uid):
        person = people.get(uid)
        return dict(name=person.display_name if person else 'Клиент',
                    freeman_id=display_freeman_id(person.freeman_id) if person else None,
                    masked_user_label=f'Пользователь •••{uid % 1000:03d}')
    def stamp(value):
        return value.replace(tzinfo=timezone.utc).isoformat()
    for edge in invited:
        rows.append(dict(id=f'registration-{edge.id}', **identity(edge.invitee_id),
                         status='registered' if edge.invitee_id in paid else 'waiting_for_purchase',
                         occurred_at=stamp(edge.created_at), payment_amount=None,
                         reward_amount=None, event_kind='registration'))
    for payment in payments:
        if payment.user_id not in allowed:
            continue
        reward = by_payment.get(payment.id)
        status = ('reward_credited' if reward.amount > 0 and reward.status == 'credited'
                  else 'reward_ineligible' if reward.status == 'ineligible' else 'cancelled') if reward else 'purchased'
        rows.append(dict(id=f'reward-{reward.id}' if reward else f'purchase-{payment.id}',
                         **identity(payment.user_id), status=status, event_kind='purchase',
                         payment_id=payment.id, occurred_at=stamp(payment.created_at),
                         payment_amount=reward.payment_amount if reward and reward.payment_amount > 0 else payment_amounts[payment.id],
                         reward_amount=reward.amount if reward else None,
                         reward_status=reward.status if reward else None))
    return sorted(rows, key=lambda row: row['occurred_at'], reverse=True)


def referrals_router(context: WebContext) -> APIRouter:
    database = context.database
    settings = context.settings
    current_user = context.current_user
    router = APIRouter()

    @router.get("/api/web/referrals")
    async def referrals(freeman_session: str | None = Cookie(default=None)):
        user = await current_user(freeman_session)
        async with database.sessions() as session:
            repo = SQLRepository(session)
            invited = list(
                await session.scalars(
                    select(Referral)
                    .where(Referral.inviter_id == user.id)
                    .order_by(Referral.id.desc())
                )
            )
            rewards = list(
                await session.scalars(
                    select(ReferralReward)
                    .where(ReferralReward.inviter_id == user.id)
                    .order_by(ReferralReward.id.desc())
                    .limit(100)
                )
            )
            earned = int(
                await session.scalar(
                    select(func.coalesce(func.sum(ReferralReward.amount), 0)).where(
                        ReferralReward.inviter_id == user.id,
                        ReferralReward.status == "credited",
                    )
                )
                or 0
            )
            invitee_ids = [item.invitee_id for item in invited]
            buyers = 0
            invitee_rows = []
            paid_ids = set()
            paid_events = []
            if invitee_ids:
                buyers = int(
                    await session.scalar(
                        select(func.count(func.distinct(Payment.user_id))).where(
                            Payment.user_id.in_(invitee_ids),
                            Payment.status.in_(
                                (
                                    "succeeded",
                                    "order_paid",
                                    "stars_paid",
                                    "balance_paid",
                                )
                            ),
                        )
                    )
                    or 0
                )
                paid_ids = set(
                    await session.scalars(
                        select(Payment.user_id).where(
                            Payment.user_id.in_(invitee_ids),
                            Payment.status.in_(
                                (
                                    "succeeded",
                                    "order_paid",
                                    "stars_paid",
                                    "balance_paid",
                                )
                            ),
                        )
                    )
                )
                paid_events = list(
                    await session.scalars(
                        select(Payment)
                        .where(
                            Payment.user_id.in_(invitee_ids),
                            Payment.status.in_(
                                (
                                    "succeeded",
                                    "order_paid",
                                    "stars_paid",
                                    "balance_paid",
                                )
                            ),
                        )
                        .order_by(Payment.created_at.desc())
                        .limit(100)
                    )
                )
                for referral in invited:
                    invitee_rows.append(
                        {
                            "masked_user_label": f"Пользователь •••{referral.invitee_id % 1000:03d}",
                            "purchased": referral.invitee_id in paid_ids,
                            "joined_at": referral.created_at.replace(
                                tzinfo=timezone.utc
                            ).isoformat(),
                        }
                    )
            # Only identities belonging to this inviter's referral edges are exposed.
            people = {person.id: person for person in await session.scalars(
                select(User).where(User.id.in_(invitee_ids)))} if invitee_ids else {}
            # Include historical/refunded purchases with a stored reward snapshot.
            linked = list(await session.scalars(select(Payment).where(
                Payment.id.in_([reward.payment_id for reward in rewards]),
                Payment.user_id.in_(invitee_ids)))) if invitee_ids and rewards else []
            history_payments = list({payment.id: payment for payment in [*paid_events, *linked]}.values())
            payment_amounts = {}
            import json
            for payment in history_payments:
                amount = payment.amount
                if payment.status == 'order_paid':
                    try:
                        order = json.loads(await repo.setting(f'order:{payment.id}', '{}'))
                    except (ValueError, TypeError):
                        order = {}
                    applied = order.get('balance_applied', 0) if isinstance(order, dict) else 0
                    if isinstance(order, dict) and order.get('amount') == amount and isinstance(applied, int) and not isinstance(applied, bool) and applied >= 0:
                        amount += applied
                payment_amounts[payment.id] = amount
            history = customer_history(invited, people, history_payments, rewards, payment_amounts)
            from app.services.referral_accounts import account_level, level, rules

            rates = await account_level(repo, user.id)
            levels = []
            config = await rules(repo)
            for count in (0, config[0], config[1]):
                current = level(count, config)
                if not levels or current["tier"] != levels[-1]["tier"]:
                    if levels:
                        levels[-1]["max_invited"] = count - 1
                    levels.append(
                        {
                            "tier": current["tier"],
                            "min_invited": count,
                            "max_invited": None,
                            "first_percent": current["first_percent"],
                            "repeat_percent": current["repeat_percent"],
                        }
                    )
            minimum = int(await repo.setting("referral_min_purchase", "0"))
            return {
                "enabled": await repo.setting("referral_enabled", "1") == "1",
                "link": f"{settings.web_base_url.rstrip('/')}/r/{display_freeman_id(user.freeman_id)}",
                "telegram_link": f"https://t.me/{settings.bot_username}?start=ref_account_{user.id}",
                **rates,
                "levels": levels,
                "available_balance": await repo.balance(user.id),
                "invited": len(invited),
                "visits": len(invited),
                "buyers": buyers,
                "visits_tracked": False,
                "conversion": round(buyers * 100 / len(invited)) if invited else 0,
                "earned": earned,
                "percent": rates["first_percent"],
                "fixed": 0,
                "minimum_purchase": minimum * 100,
                "first_only": False,
                "withdraw_min": max(1,int(await repo.setting("withdraw_min","1000")))*100,
                "withdraw_enabled": await repo.setting("withdraw_enabled","1")=="1",
                "invitees": invitee_rows,
                "history": history,
                "rewards": [
                    {
                        "id": x.id,
                        "amount": x.amount,
                        "payment_id": x.payment_id,
                        "referred_user_id": x.referred_user_id,
                        "payment_amount": x.payment_amount,
                        "percentage": x.percentage,
                        "tier": x.tier,
                        "purchase_type": x.purchase_type,
                        "status": x.status,
                        "transaction_id": x.transaction_id,
                        "created_at": x.created_at.replace(
                            tzinfo=timezone.utc
                        ).isoformat(),
                    }
                    for x in rewards
                ],
            }

    @router.get("/api/web/referrals/chart")
    async def referral_chart(
        period: str = "30d", freeman_session: str | None = Cookie(default=None)
    ):
        user = await current_user(freeman_session)
        if period not in ("7d", "30d", "all"):
            raise HTTPException(422, "Неизвестный период")
        async with database.sessions() as session:
            query = select(ReferralReward).where(
                ReferralReward.inviter_id == user.id,
                ReferralReward.status == "credited",
            )
            if period != "all":
                days = 7 if period == "7d" else 30
                first_day = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
                query = query.where(
                    ReferralReward.created_at
                    >= datetime.combine(first_day, datetime.min.time(), timezone.utc)
                )
            rewards = list(
                await session.scalars(query.order_by(ReferralReward.created_at.asc()))
            )
            buckets = {}
            for reward in rewards:
                stamp = reward.created_at.date()
                key = (
                    stamp.replace(day=1).isoformat()
                    if period == "all"
                    else stamp.isoformat()
                )
                buckets[key] = buckets.get(key, 0) + reward.amount
            return {
                "period": period,
                "points": [
                    {"date": date, "amount": amount}
                    for date, amount in sorted(buckets.items())
                ],
            }

    return router
