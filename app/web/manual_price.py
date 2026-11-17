"""Owner-confirmed manual-sale corrections, with an append-only wallet trail."""
import hashlib
import json
import secrets
import re
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from app.database.models import Payment, BalanceTransaction, AdminAudit, User, now
from app.database.repositories import SQLRepository
from app.web.subscription_transfer import owner

PREFIX = 'web_manual_price:'

def fingerprint(payment, snapshot, income):
    return hashlib.sha256(json.dumps([payment.amount,payment.status,snapshot,[(r.id,r.amount,r.user_id) for r in income]],sort_keys=True).encode()).hexdigest()

async def context(s, settings, actor, payment_id):
    await owner(s,settings,actor)
    p=await s.get(Payment,payment_id,populate_existing=True)
    repo=SQLRepository(s)
    raw=await repo.setting(f'sale_snapshot:{payment_id}','{}')
    snap=json.loads(raw)
    if not p or p.status not in ('manual_paid','manual_unpaid') or snap.get('source')!='manual' or snap.get('issuance_kind')!='manual_sale' or not snap.get('issued_at'):
        raise ValueError('Менять цену можно только у завершённой ручной продажи. Онлайн-платежи и бесплатные выдачи не изменяются.')
    uid=snap.get('recorded_by')
    wallet=await s.get(User,uid) if type(uid) is int else None
    if not wallet or not settings.is_owner(wallet.telegram_id):raise ValueError('Не найден владелец кошелька этой продажи. Нужна сверка учёта.')
    # An exact payment-bound description is the legacy link. No amount/date guesses.
    description=f'Ручная продажа #{p.id} · подписка #{p.subscription_id}'
    income=list(await s.scalars(select(BalanceTransaction).where(BalanceTransaction.description.in_((description,f'Коррекция ручной продажи #{p.id}'))).order_by(BalanceTransaction.id)))
    originals=[r for r in income if r.description==description]
    if any(r.user_id!=uid or r.type!='manual_sale_income' for r in income) or len(originals)>1:raise ValueError('Неоднозначная связь с кошельком. Нужна сверка учёта.')
    if p.status=='manual_paid' and not originals:
        # The durable manual-sales flow records a completed issuance but does
        # not post legacy wallet income. Accept only its exact payment journal.
        match=re.fullmatch(r'manual:([0-9]+):([A-Za-z0-9_-]{1,80})',p.provider_id or '')
        op=json.loads(await repo.setting(f'manual_sale:{uid}:{match[2]}','{}')) if match and int(match[1])==uid else {}
        result=op.get('result') or {}
        if op.get('version')!=1 or op.get('payment_id')!=p.id or op.get('snapshot',{}).get('recorded_by')!=uid or not result.get('done') or result.get('payment_id')!=p.id or result.get('subscription_id')!=p.subscription_id:
            raise ValueError('У продажи нет однозначной связи с кошельком или журналом выдачи. Нужна сверка учёта.')
    if p.status=='manual_unpaid' and income:raise ValueError('Неоплаченная продажа уже имеет зачисление. Нужна сверка учёта.')
    return repo,p,snap,income,uid

async def preview(s,settings,actor,payment_id,amount,reason):
    if type(amount) is not int or not 1<=amount<=100000000:raise ValueError('Укажите цену от 0,01 до 1 000 000 ₽. Бесплатная выдача — отдельная операция.')
    if not isinstance(reason,str) or not reason.strip() or len(reason)>300:raise ValueError('Укажите причину исправления, до 300 символов.')
    repo,p,snap,income,uid=await context(s,settings,actor,payment_id)
    delta=amount-sum(r.amount for r in income) if p.status=='manual_paid' else 0
    if await repo.balance(uid)+delta<0:raise ValueError('Коррекция сделает кошелёк отрицательным. Сначала нужна сверка или пополнение.')
    token=secrets.token_hex(16)
    d=dict(actor=actor.id,payment=p.id,amount=amount,old_amount=p.amount,wallet=uid,wallet_delta=delta,reason=reason.strip(),fingerprint=fingerprint(p,snap,income),until=now().timestamp()+300)
    await repo.set_setting(PREFIX+token,json.dumps(d))
    return dict(token=token,payment_id=p.id,old_amount=p.amount,amount=amount,wallet_delta=delta,wallet_id=uid,paid=p.status=='manual_paid',wallet_reconciliation=p.status=='manual_paid' and not income)

async def confirm(s,settings,actor,token):
    await owner(s,settings,actor);repo=SQLRepository(s)
    d=json.loads(await repo.setting(PREFIX+token,'{}'))
    if d.get('actor')!=actor.id:raise PermissionError('Подтверждение недоступно')
    # Same account lock as issuance, topups and withdrawal. Lock wallet before payment.
    for uid in sorted({actor.id,d['wallet']}):await s.scalar(select(User).where(User.id==uid).with_for_update())
    await s.scalar(select(Payment).where(Payment.id==d['payment']).with_for_update())
    d=json.loads(await repo.setting(PREFIX+token,'{}'))
    if d.get('done'):return d['result']
    if d['until']<now().timestamp():raise ValueError('Подтверждение устарело. Проверьте сумму заново.')
    repo,p,snap,income,uid=await context(s,settings,actor,d['payment'])
    if fingerprint(p,snap,income)!=d['fingerprint']:raise ValueError('Продажа уже изменилась. Проверьте сумму заново.')
    if await repo.balance(uid)+d['wallet_delta']<0:raise ValueError('Недостаточно средств для уменьшения кошелька.')
    if d['wallet_delta']:
        s.add(BalanceTransaction(user_id=uid,type='manual_sale_income',amount=d['wallet_delta'],description=f'Коррекция ручной продажи #{p.id}'))
    before=dict(snap);p.amount=d['amount'];snap['total']=d['amount'];snap['price_corrected_at']=now().isoformat()
    await repo.set_setting(f'sale_snapshot:{p.id}',json.dumps(snap))
    result=dict(ok=True,payment_id=p.id,amount=p.amount,wallet_delta=d['wallet_delta'])
    d.update(done=True,result=result,previous_snapshot=before)
    await repo.set_setting(PREFIX+token,json.dumps(d))
    s.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='web_manual_price',target=str(p.id),details=json.dumps(dict(old=d['old_amount'],new=p.amount,wallet=uid,delta=d['wallet_delta'],reason=d['reason'],revision=token),ensure_ascii=False)))
    await s.flush();return result

class Price(BaseModel):
    payment_id:int=Field(gt=0,strict=True)
    amount:int=Field(gt=0,le=100000000,strict=True)
    reason:str=Field(min_length=1,max_length=300)
class Confirmation(BaseModel):
    token:str=Field(pattern=r'^[a-f0-9]{32}$')
def manual_price_router(database,settings,current_user):
    router=APIRouter()
    async def actor(request):
        user=await current_user(request.cookies.get('freeman_session'))
        if not settings.is_owner(user.telegram_id):raise HTTPException(403,'Только для владельца')
        if request.headers.get('sec-fetch-site')=='cross-site' or request.headers.get('origin') not in (None,settings.web_base_url.rstrip('/')):raise HTTPException(403,'Неверный источник запроса')
        return user
    @router.post('/api/web/admin/manual-price/preview')
    async def proposed(payload:Price,request:Request):
        user=await actor(request)
        try:
            async with database.sessions.begin() as s:return await preview(s,settings,user,payload.payment_id,payload.amount,payload.reason)
        except PermissionError as e:raise HTTPException(403,str(e)) from None
        except ValueError as e:raise HTTPException(409,str(e)) from None
    @router.post('/api/web/admin/manual-price/confirm')
    async def applied(payload:Confirmation,request:Request):
        user=await actor(request)
        try:
            async with database.sessions.begin() as s:return await confirm(s,settings,user,payload.token)
        except PermissionError as e:raise HTTPException(403,str(e)) from None
        except ValueError as e:raise HTTPException(409,str(e)) from None
    return router
