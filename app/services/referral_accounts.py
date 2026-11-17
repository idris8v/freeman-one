"""Shared referral rules. A reward snapshots its rate at the paid transaction."""
from sqlalchemy import select, func, or_
from app.database.models import Referral, ReferralReward, Payment, BalanceTransaction, User

PAID = ('succeeded', 'order_paid', 'stars_paid', 'balance_paid')


DEFAULT_RULES=[6,16,15,5,20,10,30,15]
async def rules(repo):
    import json
    value=json.loads(await repo.setting('referral_tiers',str(DEFAULT_RULES)))
    return value

def level(count,config=None):
    second,third,f1,r1,f2,r2,f3,r3=config or DEFAULT_RULES
    if count>=third:return {'tier':3,'first_percent':f3,'repeat_percent':r3,'next_level_remaining':0}
    if count>=second:return {'tier':2,'first_percent':f2,'repeat_percent':r2,'next_level_remaining':third-count}
    return {'tier':1,'first_percent':f1,'repeat_percent':r1,'next_level_remaining':second-count}


async def account_level(repo, user_id):
    count=int(await repo.session.scalar(select(func.count()).select_from(Referral).where(Referral.inviter_id==user_id)) or 0)
    result={'level_basis':'invited','invited':count,**level(count,await rules(repo)), 'personal':False}
    from app.services.personal_referrals import personal
    custom=await personal(repo,user_id)
    if custom:
        result.update(first_percent=custom['first_percent'],repeat_percent=custom['repeat_percent'],personal=True,level_basis='personal',next_level_remaining=0)
    return result


async def prepare_rewards(repo, inviter_id=None):
    session=repo.session
    existing=select(ReferralReward.id).where(ReferralReward.payment_id==Payment.id).exists()
    unreversed=select(ReferralReward.id).where(ReferralReward.payment_id==Payment.id,ReferralReward.status!='reversed').exists()
    # Historical credited purchases need no work on every minute's sweep.
    pending=(Payment.status.in_(PAID) & ~existing) | ((Payment.status=='refunded') & unreversed)
    query=select(Payment).where(pending)
    if inviter_id is not None:
        query=query.where(or_(Payment.user_id.in_(select(User.id).where(User.referrer_id==inviter_id)),Payment.id.in_(select(ReferralReward.payment_id).where(ReferralReward.inviter_id==inviter_id))))
    payments=list(await session.scalars(query.order_by(Payment.id)))
    for payment in payments:
        reward=await session.scalar(select(ReferralReward).where(ReferralReward.payment_id==payment.id))
        if reward:
            if payment.status=='refunded' and reward.status!='reversed':
                if reward.amount:
                    await repo.add(BalanceTransaction(user_id=reward.inviter_id,type='referral_reversal',amount=-reward.amount,description=f'Возврат платежа #{payment.id}'))
                reward.status='reversed'
            continue
        user=await session.get(User,payment.user_id)
        if payment.status not in PAID or not user or not user.referrer_id or user.referrer_id==user.id:
            continue
        # Top-ups are money transfers, not purchases; rewarding them would pay twice.
        first=await session.scalar(select(func.min(Payment.id)).where(Payment.user_id==user.id,
            Payment.status.in_((*PAID,'refunded'))))
        rates=await account_level(repo,user.referrer_id)
        purchase_type='first' if payment.id==first else 'repeat'
        percent=rates['first_percent'] if purchase_type=='first' else rates['repeat_percent']
        payment_amount=payment.amount
        if payment.status=='order_paid':
            import json
            try: order=json.loads(await repo.setting(f'order:{payment.id}','{}'))
            except json.JSONDecodeError: order={}
            # A confirmed order can combine balance funds and external payment.
            if order.get('amount')==payment.amount and isinstance(order.get('balance_applied',0),int) and order.get('balance_applied',0)>=0:
                payment_amount+=order.get('balance_applied',0)
        eligible=await repo.setting('referral_enabled','1')=='1' and payment_amount>=int(await repo.setting('referral_min_purchase','0'))*100
        amount=payment_amount*percent//100 if eligible else 0
        reward=ReferralReward(payment_id=payment.id,inviter_id=user.referrer_id,referred_user_id=user.id,
            payment_amount=payment_amount,percentage=percent,amount=amount,tier=rates['tier'],purchase_type=purchase_type,
            status='credited' if amount else 'ineligible')
        await repo.add(reward)
        if amount:
            transaction=BalanceTransaction(user_id=user.referrer_id,type='referral_reward',amount=amount,description=f'Бонус {percent}% за платёж #{payment.id}')
            await repo.add(transaction)
            reward.transaction_id=transaction.id
