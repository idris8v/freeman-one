"""Distributed, conservative token reservations and anonymous ingress throttling."""
import json
from sqlalchemy import select,delete
from app.database.models import AIBudget,AIRateLimit,AIMessage,AIEvent,now
from app.services.customer_identity import lock_nonce
from .security import fingerprint
from datetime import timedelta

def reservation_amount(items,tools):
    # UTF-8 bytes bound input tokens conservatively; output tokens have a provider cap.
    from .images import budget_items
    return len(json.dumps([budget_items(items),tools],ensure_ascii=False).encode())+10000

async def reserve(session,settings,items,tools,context_limit=65000):
    amount=reservation_amount(items,tools)
    if amount>context_limit:raise ValueError('context_budget_exceeded')
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory=async_sessionmaker(session.bind,expire_on_commit=False)
    day=now().date().isoformat()
    async with factory.begin() as budget_session:
        await lock_nonce(budget_session,'ai:budget:'+day)
        row=await budget_session.scalar(select(AIBudget).where(AIBudget.day==day))
        if not row:row=AIBudget(day=day,reserved=0);budget_session.add(row)
        if row.reserved+amount>settings.freeman_ai_daily_token_limit:raise ValueError('daily_token_budget_exceeded')
        row.reserved+=amount
    # Unknown/provider failure retains the reservation.
    return day,amount

async def settle(session,reservation,used):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    day,amount=reservation
    async with async_sessionmaker(session.bind,expire_on_commit=False).begin() as budget_session:
        await lock_nonce(budget_session,'ai:budget:'+day)
        row=await budget_session.scalar(select(AIBudget).where(AIBudget.day==day))
        if row:row.reserved=max(0,row.reserved-amount+max(0,used))

async def throttle(session,settings,subject,limit=20):
    bucket=fingerprint(settings,'rate:'+subject+':'+str(int(now().timestamp())//60))
    await lock_nonce(session,'ai:rate:'+bucket)
    row=await session.scalar(select(AIRateLimit).where(AIRateLimit.bucket==bucket))
    if not row:row=AIRateLimit(bucket=bucket,count=0);session.add(row)
    if row.count>=limit:raise ValueError('Слишком много запросов. Подождите минуту.')
    row.count+=1

async def cleanup(session,settings):
    cutoff=now()-timedelta(days=max(7,settings.freeman_ai_retention_days))
    messages=await session.scalars(select(AIMessage).where(AIMessage.created_at<cutoff,AIMessage.text!='[Срок хранения истёк]').order_by(AIMessage.id).limit(500))
    for message in messages:
        from app.database.models import AIOwnerImage,AIOwnerFile
        await session.execute(delete(AIOwnerImage).where(AIOwnerImage.message_id==message.id))
        await session.execute(delete(AIOwnerFile).where(AIOwnerFile.message_id==message.id))
        message.text='[Срок хранения истёк]';message.actions='[]'
    events=await session.scalars(select(AIEvent).where(AIEvent.created_at<cutoff,AIEvent.details!='{}').limit(500))
    for event in events:event.details='{}'
    await session.execute(delete(AIRateLimit).where(AIRateLimit.created_at<now()-timedelta(days=1)))
    await session.execute(delete(AIBudget).where(AIBudget.created_at<now()-timedelta(days=7)))
