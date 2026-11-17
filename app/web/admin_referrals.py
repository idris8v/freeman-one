"""Owner referral dashboard. Existing ledger/rules; no automatic bank transfers."""
import hashlib, json, re
from urllib.parse import quote
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Cookie, HTTPException, Response, Request
from pydantic import BaseModel, Field, StrictInt, model_validator
from sqlalchemy import select, func, update, exists, String, case, or_, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased
from app.database.models import User, Referral, ReferralReward, Withdrawal, Payment, BalanceTransaction, AdminAudit, Delivery, AppSetting, now
from app.database.repositories import SQLRepository
from app.services.referral_accounts import rules, PAID
from app.services.offers import utc
from app.web.access import require_business_admin

TZ=ZoneInfo('Europe/Moscow')
KEYS={'referral_enabled':'1','referral_min_purchase':'0','withdraw_enabled':'1','withdraw_min':'1000'}
def iso(d):return utc(d).isoformat() if d else None

def person(u):return dict(id=u.id,name=u.display_name,freeman_id=u.freeman_id)

async def configuration(repo):
    value={k:await repo.setting(k,v) for k,v in KEYS.items()};value['tiers']=await rules(repo)
    version=hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
    return dict(enabled=value['referral_enabled']=='1',minimum_purchase=int(value['referral_min_purchase']),withdraw_enabled=value['withdraw_enabled']=='1',withdraw_min=int(value['withdraw_min']),tiers=value['tiers'],version=version)

class RulesInput(BaseModel):
    version:str=Field(pattern='^[a-f0-9]{64}$')
    enabled:bool=Field(strict=True)
    minimum_purchase:int=Field(ge=0,le=1000000,strict=True)
    withdraw_enabled:bool=Field(strict=True)
    withdraw_min:int=Field(ge=1,le=1000000,strict=True)
    tiers:list[StrictInt]=Field(min_length=8,max_length=8)
    @model_validator(mode='after')
    def valid_tiers(self):
        if any(type(v)is not int for v in self.tiers) or not 2<=self.tiers[0]<self.tiers[1]<=1000000 or any(not 0<=v<=100 for v in self.tiers[2:]):raise ValueError('Проверьте пороги ступеней и проценты от 0 до 100')
        return self

class PayoutAction(BaseModel):
    action:str=Field(pattern='^(paid|rejected)$')
    confirmed:bool=Field(default=False,strict=True)


class PersonalPreview(BaseModel):
    version: str = Field(pattern=r'^[a-f0-9]{64}$')
    first_percent: int | None = Field(default=None,ge=0,le=100,strict=True)
    repeat_percent: int | None = Field(default=None,ge=0,le=100,strict=True)
    reset: bool = Field(default=False,strict=True)
    @model_validator(mode='after')
    def rates(self):
        if self.reset and (self.first_percent is not None or self.repeat_percent is not None):raise ValueError('Для общих условий проценты не передаются')
        if not self.reset and (self.first_percent is None or self.repeat_percent is None):raise ValueError('Укажите оба процента')
        return self

class PersonalApply(BaseModel):
    token:str=Field(pattern=r'^[a-f0-9]{24}$')
    confirmed:bool=Field(default=False,strict=True)

def payout_contacts(u):
    username=(u.username or '').lstrip('@')
    telegram=('https://t.me/'+username) if re.fullmatch(r'[A-Za-z0-9_]{5,32}',username) else (f'tg://user?id={u.telegram_id}'if u.telegram_id and u.telegram_id>0 else None)
    email=u.email if u.email and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',u.email) else None
    return dict(telegram=telegram,email=email,email_url='mailto:'+quote(email,safe='@')if email else None)

def payout_requisites(raw):
    try:d=json.loads(raw)
    except (TypeError,ValueError):
        values=raw.split('|')
        d=dict(bank=values[0].strip(),account=values[1].strip(),owner_name=values[2].strip())if len(values)==3 else {}
    if not isinstance(d,dict):d={}
    method=d.get('method')
    return dict(bank=str(d.get('bank')or''),recipient=str(d.get('owner_name')or''),method=method if method in ('phone','card')else'unknown',account=str(d.get('account')or''),legacy=raw if not d else None)

async def referral_activity(s,lo,page=1,limit=20,q=''):
    buyer,inviter=aliased(User),aliased(User)
    query=select(ReferralReward,buyer,inviter).join(buyer,buyer.id==ReferralReward.referred_user_id).join(inviter,inviter.id==ReferralReward.inviter_id).where(ReferralReward.status.in_(('credited','ineligible')),ReferralReward.created_at>=lo)
    if q.strip():
        term='%'+q.strip().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        query=query.where(or_(buyer.first_name.ilike(term,escape='\\'),buyer.freeman_id.ilike(term,escape='\\'),inviter.first_name.ilike(term,escape='\\'),inviter.freeman_id.ilike(term,escape='\\')))
    total=await s.scalar(select(func.count()).select_from(query.subquery()))
    rows=(await s.execute(query.order_by(ReferralReward.created_at.desc(),ReferralReward.id.desc()).offset((page-1)*limit).limit(limit))).all()
    return dict(items=[dict(id=r.id,payment_id=r.payment_id,buyer=person(b),inviter=person(a),purchase_amount=r.payment_amount,earned=r.amount if r.status=='credited' else 0,status=r.status,created_at=iso(r.created_at))for r,b,a in rows],total=int(total or 0),page=page,limit=limit)

def admin_referrals_router(database,settings,current_user):
    router=APIRouter()
    async def owner(token):return require_business_admin(await current_user(token),settings)
    def same_origin(request):
        if request.headers.get('origin')!=settings.web_base_url.rstrip('/') or request.headers.get('sec-fetch-site')=='cross-site':raise HTTPException(403,'Откройте управление на сайте Freeman ONE')
    @router.get('/api/web/admin/referrals')
    async def overview(response:Response,days:int=30,page:int=1,q:str='',freeman_session:str|None=Cookie(default=None)):
        await owner(freeman_session);response.headers['Cache-Control']='no-store'
        if days not in (1,7,30,90,365) or not 1<=page<=100000 or len(q)>120:raise HTTPException(422,'Некорректный период или страница')
        lo=datetime.combine(now().astimezone(TZ).date()-timedelta(days=days-1),datetime.min.time(),TZ)
        async with database.sessions()as s:
            await s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            repo=SQLRepository(s);config=await configuration(repo)
            reward=ReferralReward;credited=reward.status=='credited'
            invited=await s.scalar(select(func.count()).select_from(Referral).where(Referral.created_at>=lo))
            total=await s.scalar(select(func.count()).select_from(Referral))
            earned=await s.scalar(select(func.coalesce(func.sum(reward.amount),0)).where(credited,reward.created_at>=lo))
            buyers=await s.scalar(select(func.count(func.distinct(Payment.user_id))).join(User,User.id==Payment.user_id).where(Payment.status.in_(PAID),Payment.created_at>=lo,User.referrer_id.is_not(None),User.referrer_id!=User.id))
            pending=(await s.execute(select(func.count(),func.coalesce(func.sum(Withdrawal.amount),0)).where(Withdrawal.status=='pending'))).one()
            paid=await s.scalar(select(func.coalesce(func.sum(Withdrawal.amount),0)).where(Withdrawal.status=='paid',Withdrawal.processed_at>=lo))
            inviter,invitee=aliased(User),aliased(User)
            bonuses=select(reward.inviter_id.label('inviter'),reward.referred_user_id.label('invitee'),func.sum(case((credited,reward.amount),else_=0)).label('earned'),func.count().label('purchases'),func.sum(reward.payment_amount).label('purchase_amount')).where(reward.status.in_(('credited','ineligible'))).group_by(reward.inviter_id,reward.referred_user_id).subquery()
            query=select(Referral,inviter,invitee,bonuses.c.earned,bonuses.c.purchases,bonuses.c.purchase_amount).join(inviter,inviter.id==Referral.inviter_id).join(invitee,invitee.id==Referral.invitee_id).outerjoin(bonuses,(bonuses.c.inviter==Referral.inviter_id)&(bonuses.c.invitee==Referral.invitee_id))
            if q.strip():
                term='%'+q.strip().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
                query=query.where(inviter.first_name.ilike(term,escape='\\')|inviter.freeman_id.ilike(term,escape='\\')|invitee.first_name.ilike(term,escape='\\')|invitee.freeman_id.ilike(term,escape='\\'))
            count=await s.scalar(select(func.count()).select_from(query.subquery()))
            rows=(await s.execute(query.order_by(Referral.created_at.desc(),Referral.id.desc()).offset((page-1)*20).limit(20))).all()
            day=lambda col:func.date(func.timezone('Europe/Moscow',col))
            referral_day=day(Referral.created_at);reward_day=day(reward.created_at)
            registrations={str(d):int(n)for d,n in await s.execute(select(referral_day,func.count()).where(Referral.created_at>=lo).group_by(referral_day))}
            rewards={str(d):int(n)for d,n in await s.execute(select(reward_day,func.sum(reward.amount)).where(credited,reward.created_at>=lo).group_by(reward_day))}
            series=[dict(day=(lo.date()+timedelta(days=i)).isoformat(),invited=registrations.get((lo.date()+timedelta(days=i)).isoformat(),0),earned=rewards.get((lo.date()+timedelta(days=i)).isoformat(),0))for i in range(days)]
            # Independent grouped subqueries prevent repeat purchases from multiplying invitations.
            paid_rows=select(reward.inviter_id.label('uid'),func.count(func.distinct(reward.referred_user_id)).label('buyers'),func.count().label('purchases'),func.sum(reward.payment_amount).label('revenue'),func.sum(case((credited,reward.amount),else_=0)).label('earned')).where(reward.status.in_(('credited','ineligible')),reward.created_at>=lo).group_by(reward.inviter_id).subquery()
            invited_rows=select(Referral.inviter_id.label('uid'),func.count().label('invited')).where(Referral.created_at>=lo).group_by(Referral.inviter_id).subquery()
            ranked=(await s.execute(select(User,invited_rows.c.invited,paid_rows.c.buyers,paid_rows.c.purchases,paid_rows.c.revenue,paid_rows.c.earned).outerjoin(invited_rows,invited_rows.c.uid==User.id).outerjoin(paid_rows,paid_rows.c.uid==User.id).where(User.status!='deleted',or_(invited_rows.c.invited>0,paid_rows.c.purchases>0)).order_by(func.coalesce(paid_rows.c.earned,0).desc(),func.coalesce(invited_rows.c.invited,0).desc(),User.id).limit(10))).all()
            leaders=[dict(user=person(u),invited=int(n or 0),buyers=int(b or 0),purchases=int(p or 0),revenue=int(v or 0),earned=int(e or 0)) for u,n,b,p,v,e in ranked]
            activity=(await referral_activity(s,lo,limit=5))['items']
            purchase_days={str(d):(int(n),int(v or 0))for d,n,v in await s.execute(select(reward_day,func.count(),func.sum(reward.payment_amount)).where(reward.status.in_(('credited','ineligible')),reward.created_at>=lo).group_by(reward_day))}
            for point in series:point.update(purchases=purchase_days.get(point['day'],(0,0))[0],revenue=purchase_days.get(point['day'],(0,0))[1])
            return dict(leaders=leaders,activity=activity,config=config,days=days,timezone=str(TZ),stats=dict(invited=invited,total_invited=total,earned=earned,buyers=buyers,pending=pending[0],reserved=pending[1],paid=paid),series=series,items=[dict(id=r.id,inviter=person(a),invitee=person(b),created_at=iso(r.created_at),earned=int(amount or 0),purchases=int(purchases or 0),purchase_amount=int(purchase_amount or 0))for r,a,b,amount,purchases,purchase_amount in rows],total=count,page=page,limit=20)
    @router.get('/api/web/admin/referrals/activity')
    async def activity_history(response:Response,days:int=30,page:int=1,q:str='',freeman_session:str|None=Cookie(default=None)):
        await owner(freeman_session);response.headers['Cache-Control']='no-store'
        if days not in (1,7,30,90,365) or not 1<=page<=100000 or len(q)>120:raise HTTPException(422,'Некорректный период или поиск')
        lo=datetime.combine(now().astimezone(TZ).date()-timedelta(days=days-1),datetime.min.time(),TZ)
        async with database.sessions()as session:return await referral_activity(session,lo,page=page,q=q)
    @router.get('/api/web/admin/referrals/participants')
    async def participants(response:Response,q:str='',page:int=1,segment:str='all',sort:str='invited',freeman_session:str|None=Cookie(default=None)):
        await owner(freeman_session);response.headers['Cache-Control']='no-store'
        if not 1<=page<=100000 or len(q)>120 or segment not in ('all','partners','buyers','registered') or sort not in ('invited','earned','recent'):raise HTTPException(422,'Некорректный фильтр участников')
        async with database.sessions()as s:
            invited=select(Referral.inviter_id.label('uid'),func.count().label('invited')).group_by(Referral.inviter_id).subquery()
            credited=ReferralReward.status=='credited'
            rewards=select(ReferralReward.inviter_id.label('uid'),func.count(func.distinct(ReferralReward.referred_user_id)).label('buyers'),func.count().label('purchases'),func.sum(case((credited,ReferralReward.amount),else_=0)).label('earned')).where(ReferralReward.status.in_(('credited','ineligible'))).group_by(ReferralReward.inviter_id).subquery()
            buyer_ids=select(Payment.user_id).where(Payment.status.in_(PAID)).distinct()
            parent=aliased(User);personal_setting=aliased(AppSetting);archived=aliased(AppSetting)
            query=select(User,parent,invited.c.invited,rewards.c.buyers,rewards.c.purchases,rewards.c.earned,personal_setting.value,User.id.in_(buyer_ids).label('bought')).outerjoin(parent,parent.id==User.referrer_id).outerjoin(invited,invited.c.uid==User.id).outerjoin(rewards,rewards.c.uid==User.id).outerjoin(personal_setting,personal_setting.key==('referral_personal:'+User.id.cast(String))).where(User.status!='deleted',~exists(select(archived.key).where(archived.key==('user_archived:'+User.id.cast(String)),archived.value=='1')))
            if q.strip():
                term='%'+q.strip().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
                query=query.where(or_(User.first_name.ilike(term,escape='\\'),User.freeman_id.ilike(term,escape='\\'),User.username.ilike(term,escape='\\')))
            if segment=='partners':query=query.where(invited.c.invited>0)
            elif segment=='buyers':query=query.where(User.id.in_(buyer_ids))
            elif segment=='registered':query=query.where(~User.id.in_(buyer_ids))
            total=await s.scalar(select(func.count()).select_from(query.subquery()))
            order={'invited':func.coalesce(invited.c.invited,0).desc(),'earned':func.coalesce(rewards.c.earned,0).desc(),'recent':User.created_at.desc()}[sort]
            rows=(await s.execute(query.order_by(order,User.id).offset((page-1)*20).limit(20))).all()
            from app.services.personal_referrals import decode
            items=[]
            for u,parent,n,b,p,e,raw,bought in rows:
                rates=None
                if raw:
                    try:rates=decode(raw)
                    except ValueError:pass
                items.append(dict(user=person(u),inviter=person(parent)if parent else None,invited=int(n or 0),buyers=int(b or 0),purchases=int(p or 0),earned=int(e or 0),bought=bool(bought),created_at=iso(u.created_at),personal=bool(raw),rates=rates))
            return dict(items=items,total=int(total or 0),page=page,limit=20)
    @router.post('/api/web/admin/referrals/rules')
    async def save(p:RulesInput,request:Request,freeman_session:str|None=Cookie(default=None)):
        actor=await owner(freeman_session)
        same_origin(request)
        async with database.sessions.begin()as s:
            # Same DB-only serialization order as the existing referral dispatcher.
            await s.execute(update(User).values(status=User.status))
            await s.execute(select(AppSetting).where(AppSetting.key.in_([*KEYS,'referral_tiers'])).with_for_update())
            repo=SQLRepository(s);old=await configuration(repo)
            if p.version!=old['version']:raise HTTPException(409,'Условия уже изменены. Обновите страницу перед сохранением.')
            unprocessed=await s.scalar(select(Payment.id).join(User,User.id==Payment.user_id).where(Payment.status.in_(PAID),User.referrer_id.is_not(None),User.referrer_id!=User.id,~exists(select(ReferralReward.id).where(ReferralReward.payment_id==Payment.id))).limit(1))
            if unprocessed:raise HTTPException(409,'Есть оплаченные покупки с незавершённым начислением. Дождитесь обработки и повторите сохранение.')
            for key,value in {'referral_enabled':'1'if p.enabled else'0','referral_min_purchase':str(p.minimum_purchase),'withdraw_enabled':'1'if p.withdraw_enabled else'0','withdraw_min':str(p.withdraw_min),'referral_tiers':json.dumps(p.tiers)}.items():await repo.set_setting(key,value)
            await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='web_referral_rules',target='program',details=json.dumps({'before':old,'after':p.model_dump(exclude={'version'})})))
            return await configuration(repo)
    @router.get('/api/web/admin/referrals/payouts')
    async def payouts(response:Response,status:str='pending',page:int=1,q:str='',freeman_session:str|None=Cookie(default=None)):
        await owner(freeman_session);response.headers['Cache-Control']='no-store'
        if status not in ('all','pending','paid','rejected')or not 1<=page<=100000:raise HTTPException(422,'Некорректный фильтр')
        async with database.sessions()as s:
            query=select(Withdrawal,User).join(User,User.id==Withdrawal.user_id).where(Withdrawal.status!='draft')
            if status!='all':query=query.where(Withdrawal.status==status)
            if len(q)>100:raise HTTPException(422,'Поисковый запрос слишком длинный')
            if q.strip():
                from app.services.accounts import display_freeman_id
                value=q.strip();canonical=display_freeman_id(value)if re.fullmatch(r'(?:FO-[0-9]+|FRM-[0-9]+)',value,re.I)else value
                term='%'+canonical.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
                cond=or_(User.first_name.ilike(term,escape='\\'),User.username.ilike(term,escape='\\'),User.freeman_id.ilike(term,escape='\\'),User.email.ilike(term,escape='\\'))
                if value.lstrip('#').isdigit():cond=or_(cond,Withdrawal.id==int(value.lstrip('#')))
                query=query.where(cond)

            total=await s.scalar(select(func.count()).select_from(query.subquery()))
            rows=(await s.execute(query.order_by(Withdrawal.id.desc()).offset((page-1)*20).limit(20))).all()
            return dict(items=[dict(id=w.id,user=person(u),amount=w.amount,status=w.status,created_at=iso(w.created_at),processed_at=iso(w.processed_at))for w,u in rows],total=total,page=page,limit=20)
    @router.get('/api/web/admin/referrals/payouts/{wid}')
    async def detail(wid:int,response:Response,freeman_session:str|None=Cookie(default=None)):
        actor=await owner(freeman_session);response.headers['Cache-Control']='no-store'
        async with database.sessions.begin()as s:
            w=await s.get(Withdrawal,wid)
            if not w or w.status=='draft':raise HTTPException(404,'Заявка не найдена')
            u=await s.get(User,w.user_id)
            try:details=Fernet(settings.encryption_key.encode()).decrypt(w.details.encode()).decode()
            except (InvalidToken,ValueError,UnicodeError):raise HTTPException(409,'Реквизиты недоступны. Уточните их у клиента.')
            await SQLRepository(s).add(AdminAudit(actor_telegram_id=actor.telegram_id,action='web_payout_view',target=str(wid),details='Requisites viewed by owner'))
            hold=await s.scalar(select(func.coalesce(func.sum(BalanceTransaction.amount),0)).where(BalanceTransaction.user_id==w.user_id,BalanceTransaction.type=='withdrawal_hold',BalanceTransaction.description==f'Резерв на вывод #{wid}'))
            return dict(id=w.id,user=person(u),contacts=payout_contacts(u),amount=w.amount,status=w.status,details=details,requisites=payout_requisites(details),created_at=iso(w.created_at),processed_at=iso(w.processed_at),available_balance=await SQLRepository(s).balance(u.id),reserve_confirmed=hold==-w.amount)
    @router.post('/api/web/admin/referrals/payouts/{wid}')
    async def process(wid:int,p:PayoutAction,request:Request,freeman_session:str|None=Cookie(default=None)):
        actor=await owner(freeman_session)
        same_origin(request)
        if not p.confirmed:raise HTTPException(409,'Подтвердите действие')
        async with database.sessions.begin()as s:
            repo=SQLRepository(s);w=await s.get(Withdrawal,wid)
            if not w or w.status=='draft':raise HTTPException(404,'Заявка не найдена')
            await s.execute(update(User).where(User.id==w.user_id).values(status=User.status))
            await s.refresh(w,with_for_update=True)
            if w.status==p.action:return dict(id=wid,status=w.status)
            if w.status!='pending':raise HTTPException(409,'Заявка уже обработана')
            held=await s.scalar(select(func.coalesce(func.sum(BalanceTransaction.amount),0)).where(BalanceTransaction.user_id==w.user_id,BalanceTransaction.type=='withdrawal_hold',BalanceTransaction.description==f'Резерв на вывод #{wid}'))
            if held!=-w.amount:raise HTTPException(409,'Резерв заявки требует проверки. Средства не изменены.')
            w.status=p.action;w.processed_at=now()
            if p.action=='rejected':await repo.add(BalanceTransaction(user_id=w.user_id,type='withdrawal_release',amount=w.amount,description=f'Возврат резерва #{wid}'))
            await repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='withdrawal_'+p.action,target=str(wid),details='Web manual confirmation; no bank transfer'))
            await repo.add(Delivery(dedup=f'payout:{wid}:result',user_id=w.user_id,text=f'Заявка на вывод #{wid}: '+('выплачена'if p.action=='paid'else'отклонена, резерв возвращён на баланс')))
            return dict(id=wid,status=w.status)
    async def personal_data(session,uid,actor):
        from app.services.personal_referrals import PersonalReferrals,personal
        from app.services.referral_accounts import account_level
        service=PersonalReferrals(SQLRepository(session),settings)
        u=await service.customer(uid,actor)
        from app.services.accounts import display_freeman_id
        fid = display_freeman_id(u.freeman_id) or f'FO-{u.id}'
        earned = await session.scalar(select(func.coalesce(func.sum(ReferralReward.amount),0)).where(ReferralReward.inviter_id==uid,ReferralReward.status=='credited'))
        return dict(user={**person(u),'freeman_id':fid},rates=await account_level(SQLRepository(session),uid),version=await service.fingerprint(uid),personal=await personal(SQLRepository(session),uid),
            links=dict(website=settings.web_base_url.rstrip('/')+'/r/'+quote(fid,safe=''),telegram='https://t.me/'+settings.bot_username+'?start=site_ref_'+str(uid)),earned=int(earned or 0))

    @router.get('/api/web/admin/referrals/conditions')
    async def condition_search(response:Response,q:str='',page:int=1,freeman_session:str|None=Cookie(default=None)):
        actor=await owner(freeman_session)
        if not settings.is_owner(actor.telegram_id):raise HTTPException(403,'Персональные условия доступны только администратору.')
        response.headers['Cache-Control']='no-store'
        if not 1<=page<=100000 or len(q)>100:raise HTTPException(422,'Некорректный поиск')
        if not q.strip():
            from app.services.personal_referrals import decode,key
            from app.services.accounts import display_freeman_id
            async with database.sessions()as session:
                archived=aliased(AppSetting)
                invites=select(Referral.inviter_id.label('uid'),func.count().label('invited')).group_by(Referral.inviter_id).subquery()
                earnings=select(ReferralReward.inviter_id.label('uid'),func.sum(ReferralReward.amount).label('earned')).where(ReferralReward.status=='credited').group_by(ReferralReward.inviter_id).subquery()
                query=select(User,AppSetting.value,invites.c.invited,earnings.c.earned).join(AppSetting,AppSetting.key==('referral_personal:'+User.id.cast(String))).outerjoin(invites,invites.c.uid==User.id).outerjoin(earnings,earnings.c.uid==User.id).where(AppSetting.value!='',~exists(select(archived.key).where(archived.key==('user_archived:'+User.id.cast(String)),archived.value=='1')))
                total=await session.scalar(select(func.count()).select_from(query.subquery()))
                rows=(await session.execute(query.order_by(User.id).offset((page-1)*20).limit(20))).all()
                items=[]
                for user,raw,invited,earned in rows:
                    try:rates=decode(raw);error=None
                    except ValueError:rates=None;error='Условия требуют проверки'
                    items.append(dict(**person(user),freeman_id_canonical=display_freeman_id(user.freeman_id)or f'FO-{user.id}',first_percent=rates['first_percent']if rates else None,repeat_percent=rates['repeat_percent']if rates else None,invited=int(invited or 0),earned=int(earned or 0),error=error))
                return dict(items=items,total=int(total or 0),page=page,limit=20,personal_only=True)
        if not 2<=len(q.strip())<=100:return dict(items=[])
        from app.services.accounts import display_freeman_id
        value=q.strip();value=display_freeman_id(value)if re.fullmatch(r'(?:FO-[0-9]+|FRM-[0-9]+)',value,re.I)else value
        term='%'+value.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        async with database.sessions()as session:
            query=select(User).where(User.status=='active',or_(User.freeman_id.ilike(term,escape='\\'),User.first_name.ilike(term,escape='\\'),User.username.ilike(term,escape='\\'),User.email.ilike(term,escape='\\'))).order_by(User.id).limit(20)
            users=list(await session.scalars(query));repo=SQLRepository(session)
            return dict(items=[person(u)for u in users if await repo.setting(f'user_archived:{u.id}')!='1'])

    @router.get('/api/web/admin/referrals/conditions/{uid}')
    async def condition_detail(uid:int,response:Response,freeman_session:str|None=Cookie(default=None)):
        actor=await owner(freeman_session);response.headers['Cache-Control']='no-store'
        async with database.sessions()as session:
            try:return await personal_data(session,uid,actor)
            except PermissionError as e:raise HTTPException(403,str(e))from e
            except ValueError as e:raise HTTPException(404,str(e))from e

    @router.post('/api/web/admin/referrals/conditions/{uid}/preview')
    async def condition_preview(uid:int,p:PersonalPreview,request:Request,freeman_session:str|None=Cookie(default=None)):
        actor=await owner(freeman_session);same_origin(request)
        from app.services.personal_referrals import PersonalReferrals
        from app.services.referral_accounts import level,rules
        async with database.sessions.begin()as session:
            try:
                await session.scalar(select(User).where(User.id==uid).with_for_update(nowait=True))
                data=await personal_data(session,uid,actor)
                if data['version']!=p.version:raise HTTPException(409,'Условия изменились. Откройте их заново.')
                proposal=None if p.reset else dict(version=1,first_percent=p.first_percent,repeat_percent=p.repeat_percent)
                screen=await PersonalReferrals(SQLRepository(session),settings).preview(uid,actor,proposal)
                callbacks=[item[1]for row in screen.rows for item in row if item[1].startswith('admin:clientref:apply:')]
                if len(callbacks)!=1:raise HTTPException(503,'Подтверждение временно недоступно')
                return dict(token=callbacks[0].split(':')[-1],current=data['rates'],proposed=proposal or level(data['rates']['invited'],await rules(SQLRepository(session))),personal=not p.reset)
            except PermissionError as e:raise HTTPException(403,str(e))from e
            except ValueError as e:raise HTTPException(409,str(e))from e
            except DBAPIError as e:
                if getattr(e.orig,'sqlstate',None)=='55P03'or getattr(e.orig,'pgcode',None)=='55P03':raise HTTPException(409,'Клиент сейчас занят другой операцией. Повторите позже.')from e
                raise

    @router.post('/api/web/admin/referrals/conditions/{uid}/apply')
    async def condition_apply(uid:int,p:PersonalApply,request:Request,freeman_session:str|None=Cookie(default=None)):
        actor=await owner(freeman_session);same_origin(request)
        if not p.confirmed:raise HTTPException(409,'Подтвердите изменение условий')
        from app.services.personal_referrals import PersonalReferrals
        async with database.sessions.begin()as session:
            raw=await SQLRepository(session).setting('referral_personal_confirm:'+p.token)
            try:data=json.loads(raw)if raw else{}
            except ValueError:data={}
            if data.get('user')!=uid:raise HTTPException(404,'Подтверждение не найдено')
            try:
                await PersonalReferrals(SQLRepository(session),settings).apply(p.token,actor)
                saved=json.loads(await SQLRepository(session).setting('referral_personal_confirm:'+p.token))
                if not saved.get('done'):raise HTTPException(409,'Условия пока не изменены: клиент занят другой операцией. Повторите позже.')
                return await personal_data(session,uid,actor)
            except PermissionError as e:raise HTTPException(403,str(e))from e
            except ValueError as e:raise HTTPException(409,str(e))from e
    return router

async def generate_payout_notices(database,settings):
    """Covers requests from either website or bot; same durable push queue."""
    from app.web.admin_digest import locked, PREFIX
    async with database.sessions.begin()as s:
        if not await locked(s):return
        repo=SQLRepository(s)
        key=PREFIX+'report:payout-'
        rows=await s.scalars(select(Withdrawal).where(Withdrawal.status=='pending',~exists(select(AppSetting.key).where(AppSetting.key==key+func.cast(Withdrawal.id, String)))).order_by(Withdrawal.id).limit(100))
        for w in rows:
            event=key+str(w.id);payload=dict(id='payout-'+str(w.id),kind='withdrawal',title='Новая заявка на выплату',text=f'Заявка #{w.id} · {w.amount/100:.2f} ₽',created_at=iso(w.created_at),url='/?referral_payout='+str(w.id),target='referrals')
            await repo.set_setting(event,json.dumps(payload,ensure_ascii=False))
            for row in await s.scalars(select(AppSetting).where(AppSetting.key.startswith(PREFIX+'subscription:')).limit(500)):
                meta=json.loads(row.value);user=await s.get(User,meta.get('user_id',0))
                if not user or user.status!='active' or not settings.is_owner(user.telegram_id):continue
                job=PREFIX+'delivery:payout-'+str(w.id)+':'+row.key.split(':')[-1]
                await repo.set_setting(job,json.dumps(dict(subscription=row.key,report=event,withdrawal_id=w.id,state='pending',attempts=0,available=0)))
