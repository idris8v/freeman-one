"""Explicit manual prices; corrections never call payment or VPN providers."""
import json,re,secrets
from decimal import Decimal
from datetime import timedelta
from sqlalchemy import select
from app.database.models import Payment,AppSetting,User,VlessCredential,BalanceTransaction,AdminAudit,ReferralReward,now
from app.services.admin_client_grant import rich
from app.services.screens import money

def rubles(value,allow_zero=False):
    value=value.strip().replace('₽','').strip()
    if not re.fullmatch(r'\d{1,7}(?:[.,]\d{1,2})?',value):raise ValueError('Цена в рублях: например 700 или 700.50.')
    amount=int(Decimal(value.replace(',','.'))*100)
    if not (0 if allow_zero else 1)<=amount<=100000000:raise ValueError('Цена: от '+('0' if allow_zero else '0,01')+' до 1 000 000 ₽.')
    return amount

class SalePrice:
    def __init__(self,repo,settings):self.repo,self.settings,self.s=repo,settings,repo.session
    def require(self,actor):
        if not self.settings.is_owner(actor.telegram_id):raise PermissionError('Только администратор может менять учётную цену.')
    async def payment(self,pid,actor,lock=False):
        self.require(actor)
        p=await self.s.get(Payment,pid,with_for_update=lock,populate_existing=True)
        if not p or p.status!='manual_paid' or not (p.provider_id or '').startswith(('bot-custom-sale:','bot-custom-renew:','admin-sale:')):raise ValueError('Менять можно только подтверждённую ручную продажу бота. Банковские оплаты не изменяются.')
        snap=json.loads(await self.repo.setting(f'sale_snapshot:{p.id}','{}'))
        if not snap.get('recorded_by') or snap.get('total')!=p.amount:raise ValueError('Учёт этой продажи требует проверки.')
        # Existing referral payouts must not be silently recalculated by a price edit.
        if await self.s.scalar(select(ReferralReward.id).where(ReferralReward.payment_id==p.id).limit(1)):raise ValueError('По продаже начислены реферальные бонусы. Нужна отдельная сверка; цена не изменена.')
        return p,snap
    async def render(self,action,actor):
        self.require(actor);v=action.split(':');op=v[2]
        if op=='list':
            cid=int(v[3]);c=await self.s.get(VlessCredential,cid)
            if not c:raise ValueError('Ключ не найден.')
            offset=max(0,int(v[4])) if len(v)>4 else 0
            ps=list(await self.s.scalars(select(Payment).where(Payment.subscription_id==c.subscription_id,Payment.status=='manual_paid').order_by(Payment.id.desc()).offset(offset).limit(6)))
            rows=[[(f'#{p.id} · {money(p.amount)}',f'admin:saleprice:edit:{p.id}')] for p in ps[:5] if (p.provider_id or '').startswith(('bot-custom-sale:','bot-custom-renew:','admin-sale:'))]
            if offset:rows.append([('← Предыдущие',f'admin:saleprice:list:{cid}:{max(0,offset-5)}')])
            if len(ps)>5:rows.append([('Следующие →',f'admin:saleprice:list:{cid}:{offset+5}')])
            return rich('💰 <b>Цена ручной продажи</b>\n\n'+('Выберите продажу или продление для исправления учётной цены.' if rows else 'У этого ключа нет доступных ручных продаж бота.'),rows+[[('← Ключ',f'admin:r:key:{cid}')]])
        if op=='edit':
            p,snap=await self.payment(int(v[3]),actor)
            c=await self.s.scalar(select(VlessCredential).where(VlessCredential.subscription_id==p.subscription_id))
            screen=rich(f'💰 <b>Изменить цену продажи #{p.id}</b>\n\nСейчас: {money(p.amount)}\n\nВведите новую цену в рублях. Далее будет подтверждение. Срок и ключ не меняются; повторной оплаты нет.',[[('← Продажи',f'admin:saleprice:list:{c.id}:0')]] if c else [[('← Назад','home')]])
            screen.admin_input_action=f'admin:input:saleprice:{p.id}'
            return screen
        if op=='apply':return await self.apply(v[3],actor)
        raise ValueError('Действие недоступно.')
    async def input(self,pid,value,actor):
        p,snap=await self.payment(pid,actor);amount=rubles(value);token=secrets.token_hex(10)
        await self.repo.set_setting('sale_price_change:'+token,json.dumps(dict(actor=actor.id,pid=p.id,old=p.amount,new=amount,until=now().timestamp()+600)))
        return rich(f'💰 <b>Исправить цену продажи #{p.id}?</b>\n\n{money(p.amount)} → <b>{money(amount)}</b>\n\nИзменится учётная сумма ручной продажи. Нового списания или выдачи ключа не будет.',[[('✅ Сохранить цену','admin:saleprice:apply:'+token)],[('← Изменить',f'admin:saleprice:edit:{p.id}')]])
    async def apply(self,token,actor):
        self.require(actor)
        if not re.fullmatch('[0-9a-f]{20}',token):raise ValueError('Подтверждение недоступно.')
        row=await self.s.get(AppSetting,'sale_price_change:'+token,with_for_update=True,populate_existing=True)
        d=json.loads(row.value) if row else {}
        if d.get('actor')!=actor.id:raise PermissionError('Подтверждение другого администратора.')
        if d.get('done'):return rich('Цена уже исправлена. Повторной операции нет.',[[('← Продажа',f'admin:saleprice:edit:{d["pid"]}')]])
        if d['until']<now().timestamp():raise ValueError('Подтверждение устарело.')
        await self.s.get(User,actor.id,with_for_update=True)
        p,snap=await self.payment(d['pid'],actor,True)
        if p.amount!=d['old']:raise ValueError('Цена уже изменилась. Откройте продажу заново.')
        income=[]
        if p.provider_id.startswith('admin-sale:'):description=f'Ручная продажа #{p.id} · подписка #{p.subscription_id}'
        elif p.provider_id.startswith('bot-custom-renew:'):description=f'Ручное продление подписки #{p.subscription_id}'
        else:description=None
        if description:
            income=list(await self.s.scalars(select(BalanceTransaction).where(BalanceTransaction.user_id==snap['recorded_by'],BalanceTransaction.type=='manual_sale_income',BalanceTransaction.description==description,BalanceTransaction.created_at>=p.created_at-timedelta(seconds=30),BalanceTransaction.created_at<=p.created_at+timedelta(seconds=30))))
            if len(income)!=1:raise ValueError('Не удалось однозначно связать учёт дохода. Цена не изменена.')
        delta=d['new']-p.amount
        if not delta:
            return rich('Цена уже составляет '+money(p.amount)+'.',[[('← Продажа',f'admin:saleprice:edit:{p.id}')]])
        if delta and income:await self.repo.add(BalanceTransaction(user_id=snap['recorded_by'],type='manual_sale_income',amount=delta,description=f'Коррекция цены ручной продажи #{p.id}'))
        await self.repo.set_setting(f'sale_price_original:{p.id}',await self.repo.setting(f'sale_price_original:{p.id}') or str(p.amount))
        p.amount=d['new'];snap.update(total=d['new'],price_corrected_at=now().isoformat(),price_corrected_by=actor.id)
        await self.repo.set_setting(f'sale_snapshot:{p.id}',json.dumps(snap))
        await self.repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='manual_sale_price',target=str(p.id),details=f'{d["old"]} -> {d["new"]} kopecks; no charge or provisioning'))
        from app.services.activity import record
        from app.services.bot_admin_checkout import today_summary
        from sqlalchemy.exc import SQLAlchemyError
        try:
            revenue,_=await today_summary(self.repo)
        except SQLAlchemyError:
            revenue=None
        await record(self.repo,f'sale.corrected:{p.id}:{token}',json.dumps({
            'old':d['old'],'new':p.amount,'revenue':revenue,'actor':actor.id,
        }))
        d['done']=True;await self.repo.set_setting(row.key,json.dumps(d))
        text='✅ Цена ручной продажи исправлена: '+money(p.amount)
        if revenue is not None:text+='\n\n📊 Выручка за сегодня: <b>'+money(revenue)+'</b>'
        return rich(text,[[('← Продажа',f'admin:saleprice:edit:{p.id}')]])
