"""Telegram-only, prorated allowance increase. No new key or extension is sold."""
import json,re,secrets
from datetime import timedelta
from decimal import Decimal,ROUND_CEILING
from html import escape
from sqlalchemy import select,update
from aiogram.types import InputRichMessage
from app.database.models import User,Subscription,AccessKey,VlessCredential,VlessRenewal,Payment,BalanceTransaction,AppSetting,now
from app.services.screens import Screen,money,time_remaining
from app.services.vless_creation import utc
from app.services.vless_renewal import locked_context,operation_id,valid_term
from app.services.bot_catalog import available,devices_label

TTL=600
PREFIX='bot_upgrade:'

def rich(text,rows):
    from app.bot.rich_copy import text_blocks
    return Screen(text,rows,rich=InputRichMessage(blocks=text_blocks(text),skip_entity_detection=True),structured=True)

def difference(old_price,new_price,months,seconds):
    if type(months) is not int or months<1 or type(old_price) is not int or type(new_price) is not int or not 0<=old_price<new_price<=100_000_000 or seconds<=0:
        raise ValueError('Для этого тарифа автоматический перерасчёт недоступен. Обратитесь в поддержку.')
    return int((Decimal(new_price-old_price)*Decimal(str(seconds))/Decimal(months*30*86400)).to_integral_value(rounding=ROUND_CEILING))

def failure(action,error):
    text=str(error) if isinstance(error,ValueError) else 'Выбор устарел. Откройте тариф заново.'
    return rich('⚠️ <b>Улучшение тарифа</b>\n\n'+escape(text),[[('🔄 Повторить',action)],[('💬 Поддержка','support')],[('← Мои ключи','subscriptions')]])

class BotUpgrade:
    def __init__(self,repo,settings,*,source='telegram'):
        if source not in ('telegram','website'):raise ValueError('Unknown upgrade source')
        self.repo,self.settings,self.session,self.source=repo,settings,repo.session,source

    async def context(self,user,sid):
        sub=await self.repo.subscription(user.id,sid)
        c=await self.session.scalar(select(VlessCredential).where(VlessCredential.subscription_id==sid,VlessCredential.user_id==user.id)) if sub else None
        if not c:raise ValueError('Для этой подписки автоматическое улучшение пока недоступно.')
        sub,key,c=await locked_context(self.repo,c)
        if user.status!='active' or sub.kind=='trial' or utc(sub.expires_at)<=now() or c.status!='active' or key.status!='active' or await self.repo.setting(f'subscription_removed:{sid}')=='1' or await self.repo.setting(f'subscription_transferred:{sid}')=='1':
            raise ValueError('Улучшение доступно только для вашей активной платной подписки. Для истёкшей подписки выберите продление.')
        if c.device_limit<=0 or c.device_limit!=sub.device_limit:raise ValueError('Параметры подписки требуют проверки в поддержке.')
        from app.services.recurring_cards import unsettled_card
        if await unsettled_card(self.repo,sid):raise ValueError('Сначала дождитесь завершения текущего автоплатежа.')
        return sub,key,c

    async def rate(self,user,tariff):
        from app.services.pricing import base_quote
        from app.services.catalog import public_price
        value,_,personal=await base_quote(self.repo,user.id,tariff)
        return value if personal else min(value,await public_price(self.repo,'vpn',tariff.id,tariff.price,True))

    async def plans(self,user,sub):
        base=await self.repo.tariff_by_id(sub.tariff_id) if sub.tariff_id else None
        # A custom/free period has no defined price basis. Never invent credit.
        if not base or base.device_limit!=sub.device_limit:raise ValueError('У этой подписки индивидуальные условия. Для улучшения обратитесь в поддержку; обычное продление доступно ниже.')
        plans=[t for t in await available(self.repo) if t.months==base.months and sub.device_limit<t.device_limit<=100]
        return base,plans

    async def saved(self,user,token):
        if not re.fullmatch('[0-9a-f]{20}',token):raise ValueError('Расчёт недоступен.')
        row=await self.session.scalar(select(AppSetting).where(AppSetting.key==PREFIX+token).execution_options(populate_existing=True))
        data=json.loads(row.value) if row else {}
        if data.get('user')!=user.id:raise ValueError('Расчёт недоступен.')
        return data

    async def save(self,token,data):await self.repo.set_setting(PREFIX+token,json.dumps(data))

    async def pending(self,user,sid):
        token=await self.repo.setting(f'bot_upgrade_open:{user.id}:{sid}')
        if token:
            data=await self.saved(user,token)
            if data.get('held') and not data.get('done'):return token,data
        return None

    async def render(self,action,user):
        p=action.split(':');op=p[1]
        if op in ('show','apply','fund','cancel'):
            token=p[2];data=await self.saved(user,token)
            if op=='apply':return await self.apply(user,token,data)
            if op=='cancel':return await self.cancel(user,token,data)
            if data.get('done'):return self.success(data)
            if op=='fund':
                if data.get('held'):return await self.summary(user,token,data)
                if data['until']<now().timestamp():return await self.render(f'up:quote:{data["sid"]}:{data["target"]}',user)
                from app.services.topups import TopupService
                shortfall=max(100,data['amount']-await self.repo.balance(user.id))
                screen=await TopupService(self.repo,self.settings).prepare(user,shortfall)
                for row in screen.rows:
                    for _,target in row:
                        if target.startswith('topup_pay:'):await self.repo.set_setting('bot_upgrade_topup:'+target.split(':')[1],token)
                screen.rows.append([('← К расчёту',f'up:show:{token}')])
                return screen
            return await self.summary(user,token,data)
        sid=int(p[2]);sub,key,c=await self.context(user,sid)
        pending=await self.pending(user,sid)
        if pending:return await self.summary(user,*pending)
        base,plans=await self.plans(user,sub)
        old=await self.rate(user,base)
        if op=='menu':
            options=[]
            for t in plans:
                new=await self.rate(user,t)
                if new>old:options.append((t,difference(old,new,base.months,(utc(sub.expires_at)-now()).total_seconds())))
            offset=max(0,int(p[3])) if len(p)>3 else 0;offset=min(offset,max(0,(len(options)-1)//5*5))
            rows=[[(f'✨ {devices_label(t.device_limit)} · доплата {money(amount)}',f'up:quote:{sid}:{t.id}')] for t,amount in options[offset:offset+5]]
            if offset:rows.append([('← Предыдущие',f'up:menu:{sid}:{max(0,offset-5)}')])
            if offset+5<len(options):rows.append([('Следующие →',f'up:menu:{sid}:{offset+5}')])
            text=f'✨ <b>Улучшить тариф</b>\n\nСейчас: {devices_label(sub.device_limit)}\nОсталось: {time_remaining(sub.expires_at)}\n\nВыберите больше устройств. Доплата — разница действующих цен только за оставшийся срок. Дата окончания и ссылка ключа сохранятся. Разовые промокоды и предложения не применяются.'
            if not options:text+='\n\nДля этого тарифа нет доступного повышения числа устройств.'
            return rich(text,rows+[[('🔄 Увеличить срок',f'renew:{sid}')],[('← Ключ',f'sub:{sid}')]])
        if op!='quote':raise ValueError('Действие недоступно.')
        target=next((t for t in plans if t.id==int(p[3])),None)
        if not target:raise ValueError('Этот вариант больше недоступен.')
        new=await self.rate(user,target);at=now();seconds=(utc(sub.expires_at)-at).total_seconds();amount=difference(old,new,base.months,seconds)
        token=secrets.token_hex(10)
        data=dict(source=self.source,user=user.id,sid=sid,cid=c.id,base=base.id,target=target.id,months=base.months,old=old,new=new,devices=sub.device_limit,wanted=target.device_limit,expiry=utc(sub.expires_at).isoformat(),amount=amount,seconds=seconds,until=(at+timedelta(seconds=TTL)).timestamp(),held=False,done=False)
        await self.save(token,data)
        return await self.summary(user,token,data)

    async def summary(self,user,token,d):
        if d['done']:return self.success(d)
        text=(f'✨ <b>Расчёт улучшения</b>\n\n{devices_label(d["devices"])} → <b>{devices_label(d["wanted"])}</b>'
              f'\nДействует до: {utc(__import__("datetime").datetime.fromisoformat(d["expiry"])).strftime("%d.%m.%Y")}'
              f'\n\nЦена за {d["months"]} мес.: {money(d["old"])} → {money(d["new"])}'
              f'\nОстаток при расчёте: {d["seconds"]/86400:.2f} дн.\n<b>Доплата: {money(d["amount"])}</b>'
              '\n\nСрок, ссылка и подключённые устройства сохраняются. Сумма фиксируется на 10 минут. Если автопродление включено, оно будет выключено — настройте его заново для нового тарифа.')
        if d.get('held'):
            text+='\n\n⏳ Панель ещё проверяется. Сумма зарезервирована, повторной оплаты не нужно. Нажмите «Проверить улучшение».'
            rows=[[('🔄 Проверить улучшение',f'up:apply:{token}')]]
        elif d['until']<now().timestamp():
            rows=[[('🔄 Обновить расчёт',f'up:quote:{d["sid"]}:{d["target"]}')]]
        else:
            balance=await self.repo.balance(user.id);text+=f'\n\nБаланс: {money(balance)}'
            rows=[[('✅ Улучшить за '+money(d['amount']),f'up:apply:{token}')]] if balance>=d['amount'] else [[('💳 Пополнить недостающее',f'up:fund:{token}')],[('🔄 Проверить баланс',f'up:show:{token}')]]
        if d.get('held'):rows.append([('↩️ Проверить возможность отмены',f'up:cancel:{token}')])
        else:rows.append([('← Изменить выбор',f'up:menu:{d["sid"]}')])
        return rich(text,rows)

    def success(self,d):
        return rich(f'✅ <b>Тариф улучшен</b>\n\nТеперь: {devices_label(d["wanted"])}\nДоплата: {money(d["amount"])}\n\nДата окончания и ключ сохранены. Если использовали автопродление, настройте его заново.',[[('🔑 Открыть ключ',f'sub:{d["sid"]}')],[('🔁 Автопродление',f'autorenew:{d["sid"]}')]])

    async def release(self,user,token,d):
        if d.get('held'):
            await self.repo.add(BalanceTransaction(user_id=user.id,type='upgrade_release',amount=d['amount'],description=f'Возврат резерва улучшения #{d["sid"]}'))
            d['held']=False
            await self.save(token,d)

    async def cancel(self,user,token,d):
        await self.session.execute(update(User).where(User.id==user.id).values(status=User.status))
        d=await self.saved(user,token)
        if d.get('done'):return self.success(d)
        op=await self.session.get(VlessRenewal,operation_id(user.id,'bot-upgrade:'+token),populate_existing=True)
        if op and op.state!='failed':raise ValueError('Изменение уже проверяется панелью. Завершите его проверку, отменять или оплачивать заново нельзя.')
        await self.release(user,token,d);d['until']=0;await self.save(token,d)
        return rich('Расчёт отменён. Сумма доступна на балансе.',[[('← Ключ',f'sub:{d["sid"]}')]])

    async def apply(self,user,token,d):
        # All wallet consumers serialize on the same customer row.
        current=await self.session.scalar(select(User).where(User.id==user.id).with_for_update().execution_options(populate_existing=True))
        d=await self.saved(current,token);ref='bot-upgrade:'+token
        if d.get('done'):return self.success(d)
        previous=await self.session.scalar(select(Payment).where(Payment.provider_id==ref))
        if previous:raise ValueError('Оплата уже записана. Откройте ключ заново; повторное списание не выполнено.')
        sub,key,c=await self.context(current,d['sid'])
        if utc(sub.expires_at).isoformat()!=d['expiry'] or sub.device_limit!=d['devices'] or c.id!=d['cid']:raise ValueError('Подписка изменилась. Не подтверждайте старый расчёт; откройте улучшение заново.')
        operation=await self.session.get(VlessRenewal,operation_id(user.id,ref),populate_existing=True)
        recovering=operation and operation.state!='failed'
        existing=await self.pending(user,sub.id)
        if existing and existing[0]!=token:return await self.summary(user,*existing)
        if not recovering:
            payments=await self.session.scalars(select(Payment).where(Payment.user_id==user.id,Payment.status.in_(('order_created','order_pending','order_processing','stars_created','stars_pending','stars_processing'))))
            for payment in payments:
                prefix='stars_order:' if payment.status.startswith('stars_') else 'order:'
                order=json.loads(await self.repo.setting(prefix+str(payment.id),'{}'))
                if order.get('sid')==sub.id:raise ValueError('Сначала завершите или отмените уже созданную оплату продления этой подписки.')
            if d['until']<now().timestamp():raise ValueError('Расчёт устарел. Обновите его перед оплатой.')
            base,plans=await self.plans(current,sub);target=next((t for t in plans if t.id==d['target']),None)
            if not target or base.id!=d['base'] or await self.rate(current,base)!=d['old'] or await self.rate(current,target)!=d['new']:raise ValueError('Цены или тарифы изменились. Откройте новый расчёт.')
        if not d.get('held'):
            if await self.repo.balance(user.id)<d['amount']:return await self.summary(user,token,d)
            hold=await self.repo.add(BalanceTransaction(user_id=user.id,type='upgrade_hold',amount=-d['amount'],description=f'Резерв улучшения #{sub.id}'))
            d['held']=True;d['hold_id']=hold.id
            await self.save(token,d)
            await self.repo.set_setting(f'bot_upgrade_open:{user.id}:{sub.id}',token)
        from app.services.vless_provision import control
        try:
            await control(self.repo,self.settings,c,'extend',sub.expires_at,renewal=dict(operation_key=ref,request=dict(kind='bot_device_upgrade',target=d['target'],amount=d['amount']),devices=d['wanted']))
        except ValueError:
            operation=await self.session.get(VlessRenewal,operation_id(user.id,ref),populate_existing=True)
            if not operation or operation.state=='failed':await self.release(user,token,d)
            if d.get('held'):return await self.summary(user,token,d)
            return rich('⚠️ Панель не подтвердила улучшение. Списание не выполнено, сумма доступна на балансе.',[[('🔄 Повторить',f'up:apply:{token}')],[('← Ключ',f'sub:{sub.id}')]])
        # Durable remote confirmation and the debit receipt share this commit.
        hold=await self.session.get(BalanceTransaction,d['hold_id'])
        hold.type='subscription_upgrade';hold.description=f'Улучшение подписки #{sub.id}: {d["devices"]} → {d["wanted"]} устр.'
        c.device_limit=sub.device_limit=d['wanted'];sub.tariff_id=d['target']
        from app.services.subscription_labels import refresh
        await refresh(self.repo,sub,key)
        await self.repo.set_setting(f'auto_renew:{sub.id}','0')
        payment=await self.repo.add(Payment(user_id=user.id,subscription_id=sub.id,amount=d['amount'],status='balance_paid',provider_id=ref))
        from app.web.admin_reports import record_sale
        await record_sale(self.repo,payment,0,d['wanted'],renewal=True,source=d.get('source','telegram'))
        snapshot=json.loads(await self.repo.setting(f'sale_snapshot:{payment.id}'))
        snapshot.update(upgrade=True,tariff_name=f'Улучшение {d["devices"]} → {d["wanted"]}',tariff_id=d['target'])
        await self.repo.set_setting(f'sale_snapshot:{payment.id}',json.dumps(snapshot))
        d['done']=True;d['held']=False;d['payment']=payment.id;await self.save(token,d)
        return self.success(d)
