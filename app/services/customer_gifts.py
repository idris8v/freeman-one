"""Paid customer VPN gifts: verified payment creates an invitation, never a buyer key."""
import json,re,secrets,uuid
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode
from sqlalchemy import select,cast,String,literal
from app.database.models import User,Payment,AppSetting,BalanceTransaction,ChatNotice,now
from app.services.bot_gift_links import gift_key,styled,BotGiftLinks
from app.services.bot_catalog import available,devices_label,page_offset
from app.services.screens import money,duration
from app.services.vless_creation import utc
from app.services.topups import YooKassa

ORDER='customer_gift_order:'
QUOTE='customer_gift_quote:'

class CustomerGifts:
    def __init__(self,repo,settings,provider=None):self.repo,self.settings,self.session=repo,settings,repo.session;self.provider=provider or YooKassa(settings)
    @property
    def enabled(self):return bool(self.settings.yookassa_shop_id and self.settings.yookassa_secret_key)
    async def lock(self,user):
        fresh=await self.session.scalar(select(User).where(User.id==user.id).with_for_update().execution_options(populate_existing=True))
        if not fresh or fresh.status!='active':raise ValueError('Аккаунт недоступен.')
    async def order(self,pid,user=None):
        p=await self.session.get(Payment,pid,populate_existing=True)
        row=await self.session.get(AppSetting,ORDER+str(pid),populate_existing=True)
        d=json.loads(row.value) if row else {}
        if not p or not d or d.get('buyer')!=p.user_id or user and p.user_id!=user.id:raise ValueError('Подарок недоступен.')
        return p,d
    async def render(self,action,user):
        p=action.split(':');op=p[1]
        administrator=self.settings.is_owner(user.telegram_id)
        if administrator and op in ('home','devices','tariff','balance','invoice'):
            return await self.render('cg:hub',user)
        if op=='hub':
            if not administrator:return await self.render('cg:home',user)
            return styled('🎁 <b>Подарить VPN другу</b>\n\nСоздайте бесплатный подарок или откройте свои приглашения.',[[('🎁 Создать подарок','admin:input:giftlink:vpn')],[('📨 Мои подарки','admin:giftlink:list:vpn:0')],[('← Купить подписку','buy')]])
        if op in ('home','devices'):
            tariffs=await available(self.repo)
            if op=='home':
                groups=sorted({t.device_limit for t in tariffs});offset=page_offset(int(p[2]) if len(p)>2 else 0,len(groups))
                rows=[[(f'📱 {devices_label(n)}',f'cg:devices:{n}')] for n in groups[offset:offset+5]]
                if offset:rows.append([('← Предыдущие',f'cg:home:{offset-5}')])
                if offset+5<len(groups):rows.append([('Следующие →',f'cg:home:{offset+5}')])
                rows+=[[('🎁 Мои подарки','cg:list:0')],[('← Купить подписку','buy')]]
                return styled('🎁 <b>Купить VPN в подарок</b>\n\nШаг 1 из 2 — выберите количество устройств.\n\nПосле оплаты получите приглашение для друга. Срок начнётся при получении, ключ выдаётся на аккаунт получателя.',rows)
            n=int(p[2]);terms=[t for t in tariffs if t.device_limit==n];terms.sort(key=lambda t:(t.months,t.id))
            offset=page_offset(int(p[3]) if len(p)>3 else 0,len(terms));rows=[]
            from app.services.pricing import quote
            for t in terms[offset:offset+5]:rows.append([(duration(t.months)+' · '+money((await quote(self.repo,user.id,t,0))[0]),f'cg:tariff:{t.id}')])
            if offset:rows.append([('← Предыдущие сроки',f'cg:devices:{n}:{offset-5}')])
            if offset+5<len(terms):rows.append([('Следующие сроки →',f'cg:devices:{n}:{offset+5}')])
            rows.append([('← Устройства','cg:home')])
            return styled(f'🎁 <b>Подарок · {devices_label(n)}</b>\n\nШаг 2 из 2 — выберите срок. Срок будет отсчитываться после получения другом.',rows)
        if op=='tariff':
            t=next((t for t in await available(self.repo) if t.id==int(p[2])),None)
            if not t:raise ValueError('Тариф больше недоступен.')
            from app.services.pricing import quote
            from app.services.billing import purchase_limits
            devices,traffic=await purchase_limits(self.repo,t);total=(await quote(self.repo,user.id,t,0))[0]
            token=secrets.token_hex(10)
            d=dict(buyer=user.id,tariff=t.id,months=t.months,devices=devices,traffic=traffic,total=total,base_price=t.price,until=now().timestamp()+600)
            await self.repo.set_setting(QUOTE+token,json.dumps(d))
            rows=[]
            if await self.repo.balance(user.id)>=total:rows.append([('💰 Оплатить с баланса · '+money(total),f'cg:balance:{token}')])
            if self.enabled and total>=100 and await self.repo.setting('payment_provider:yookassa','1')=='1':rows.append([('💳 Перейти к оплате · '+money(total),f'cg:invoice:{token}')])
            if not rows:rows.append([('💰 Пополнить баланс','deposit')])
            rows.append([('← Сроки',f'cg:devices:{devices}')])
            return styled(f'🎁 <b>Подарок другу</b>\n\n{devices_label(devices)} · {duration(t.months)}\n<b>Стоимость: {money(total)}</b>\n\nКлюч будет выдан другу после получения подарка. Его срок не расходуется, пока подарок ждёт активации.',rows,{target:{'style':'success' if ':balance:' in target else 'primary'} for row in rows for _,target in row if target.startswith(('cg:balance:','cg:invoice:'))})
        if op in ('balance','invoice'):
            payment=await self.prepare(p[2],user,op)
            return await self.receipt(payment.id,user)
        if op in ('view','pay','check'):
            payment,d=await self.order(int(p[2]),user)
            if op=='pay' and payment.status=='gift_created':
                if not self.enabled or await self.repo.setting('payment_provider:yookassa','1')!='1':raise ValueError('Оплата временно недоступна.')
                await self.lock(user)
                payment,d=await self.order(payment.id,user)
                if not payment.provider_id:
                    if utc(payment.created_at)<now()-timedelta(hours=23):
                        await self.expire(payment.id,user)
                        return await self.receipt(payment.id,user)
                    data=await self.provider.create(payment,d['idem'],description=f'VPN в подарок Freeman ONE #{payment.id}',payment_method='sbp')
                    url=data.get('confirmation',{}).get('confirmation_url','')
                    if not data.get('id') or not url.startswith('https://'):raise ValueError('ЮKassa не подтвердила счёт. Повторите этот же запрос, не создавая новый.')
                    payment.provider_id=data['id'];payment.status='gift_pending';d['url']=url
                    await self.repo.set_setting(ORDER+str(payment.id),json.dumps(d))
            if op=='check' and payment.status=='gift_pending':await self.credit(payment,await self.provider.get(payment.provider_id))
            return await self.receipt(payment.id,user)
        if op=='rotate':
            await self.order(int(p[2]),user)
            return styled('🔗 <b>Перевыпустить приглашение?</b>\n\nСтарая ссылка перестанет работать. Оплаченный подарок сохранится, новая ссылка появится здесь.',[[('Перевыпустить',f'cg:rotateok:{p[2]}')],[('← Подарок',f'cg:view:{p[2]}')]])
        if op=='rotateok':return await self.rotate(int(p[2]),user)
        if op=='list':
            offset=max(0,int(p[2])) if len(p)>2 else 0
            rows=[]
            query=select(Payment,AppSetting.value).join(AppSetting,AppSetting.key==(literal(ORDER)+cast(Payment.id,String))).where(Payment.user_id==user.id).order_by(Payment.id.desc())
            gifts=[(pay,json.loads(raw)) for pay,raw in await self.session.execute(query.offset(offset).limit(6))]
            more=len(gifts)>5
            for pay,d in gifts[:5]:
                state='⏳ Ожидает оплаты'
                if d.get('ready'):
                    gift=json.loads(await self.repo.setting(gift_key(d['token']),'{}'))
                    state='✅ Получен' if gift.get('user') else '🎁 Ожидает друга'
                if pay.status=='gift_canceled':state='Отменён'
                if pay.status=='refunded':state='↩️ Возврат'
                rows.append([(f'{state} · {devices_label(d["devices"])} · {duration(d["months"])}',f'cg:view:{pay.id}')])
            if offset:rows.append([('← Предыдущие',f'cg:list:{offset-5}')])
            if more:rows.append([('Следующие →',f'cg:list:{offset+5}')])
            rows+=[[('🎁 Купить подарок','cg:home')],[('← Подарить другу','cg:hub')]]
            return styled('🎁 <b>Мои подарки</b>\n\n'+('Выберите подарок.' if gifts else 'Вы ещё не покупали подарки.'),rows)
        raise ValueError('Действие недоступно.')

    async def prepare(self,token,user,method):
        if not re.fullmatch('[0-9a-f]{20}',token):raise ValueError('Расчёт недоступен.')
        await self.lock(user)
        raw=await self.session.get(AppSetting,QUOTE+token,populate_existing=True);d=json.loads(raw.value) if raw else {}
        if d.get('buyer')!=user.id:raise ValueError('Расчёт другого клиента.')
        if d.get('payment'):
            payment,_=await self.order(d['payment'],user);return payment
        if d['until']<now().timestamp():raise ValueError('Расчёт устарел. Выберите тариф заново.')
        t=next((t for t in await available(self.repo) if t.id==d['tariff']),None)
        from app.services.pricing import quote
        from app.services.billing import purchase_limits
        if not t or t.months!=d['months'] or t.device_limit!=d['devices'] or (await quote(self.repo,user.id,t,0))[0]!=d['total'] or (await purchase_limits(self.repo,t))[1]!=d['traffic']:raise ValueError('Тариф изменился. Откройте новый расчёт.')
        if method=='balance' and await self.repo.balance(user.id)<d['total']:raise ValueError('Недостаточно средств. Оплата не выполнена.')
        if method=='invoice' and (not self.enabled or d['total']<100 or await self.repo.setting('payment_provider:yookassa','1')!='1'):raise ValueError('Оплата через ЮKassa недоступна.')
        payment=await self.repo.add(Payment(user_id=user.id,amount=d['total'],status='gift_created',provider_id='customer-gift-wallet:'+token if method=='balance' else None))
        d.update(payment=payment.id,method=method,token=secrets.token_urlsafe(24),idem=str(uuid.uuid4()))
        await self.repo.set_setting(ORDER+str(payment.id),json.dumps(d));await self.repo.set_setting(QUOTE+token,json.dumps(d))
        from app.services.offers import reserve
        await reserve(self.repo,payment,t,0,d['total'])
        if method=='balance':
            await self.repo.add(BalanceTransaction(user_id=user.id,type='gift_purchase',amount=-d['total'],description=f'VPN в подарок #{payment.id}'))
            await self.complete(payment,d,'balance_paid')
        return payment

    async def expire(self,pid,user):
        await self.lock(user);payment,d=await self.order(pid,user)
        if payment.status=='gift_created' and not payment.provider_id and utc(payment.created_at)<now()-timedelta(hours=23):
            payment.status='gift_canceled'
            from app.services.offers import release
            await release(self.repo,payment)

    async def complete(self,payment,d,status):
        if d.get('ready'):return
        gift=dict(actor=d['buyer'],buyer=d['buyer'],paid=True,payment=payment.id,kind='vpn',months=d['months'],tariff=d['tariff'],traffic=d['traffic'],devices=d['devices'],hours=d['months']*720,label=duration(d['months']),status='open',expires='9999-01-01T00:00:00+00:00')
        await self.repo.set_setting(gift_key(d['token']),json.dumps(gift))
        payment.status=status;d['ready']=True
        await self.repo.set_setting(ORDER+str(payment.id),json.dumps(d))
        from app.services.offers import consume
        await consume(self.repo,payment)
        await self.repo.set_setting(f'sale_snapshot:{payment.id}',json.dumps(dict(kind='vpn',source='telegram',gift=True,renewal=False,months=d['months'],devices=d['devices'],total=payment.amount,base_price=d['base_price'],tariff_id=d['tariff'],tariff_name='Подарочная VPN-подписка',paid_at=now().isoformat(),test=bool(d.get('test',False)))))

    async def credit(self,payment,data):
        user=await self.session.get(User,payment.user_id)
        await self.lock(user);payment,d=await self.order(payment.id,user)
        if d.get('ready'):return True
        if payment.amount!=d['total']:raise ValueError('Условия заказа повреждены.')
        if data.get('id')!=payment.provider_id:raise ValueError('Не совпадает платёж подарка.')
        if data.get('status')=='canceled':
            payment.status='gift_canceled'
            from app.services.offers import release
            await release(self.repo,payment);return False
        if data.get('status')!='succeeded' or data.get('paid') is not True:return False
        amount=data.get('amount',{});meta=data.get('metadata',{})
        if payment.status!='gift_pending' or amount.get('currency')!='RUB' or Decimal(amount.get('value','0'))*100!=payment.amount or meta.get('freeman_payment')!=str(payment.id) or meta.get('freeman_user')!=str(payment.user_id) or bool(data.get('test',False))!=self.settings.yookassa_test_mode:raise ValueError('Данные оплаты подарка не прошли проверку.')
        d['test']=bool(data.get('test',False));await self.complete(payment,d,'order_paid')
        if user.telegram_id:await self.repo.add(ChatNotice(dedup=f'customer_gift:{payment.id}',target_id=user.telegram_id,kind='client',text='🎁 Подарок оплачен! Откройте «Купить подписку → Мои подарки», чтобы отправить приглашение другу.'))
        return True

    async def receipt(self,pid,user):
        payment,d=await self.order(pid,user)
        if not d.get('ready'):
            if payment.status=='gift_canceled':return styled('Оплата подарка отменена. Доступ не выдавался.',[[('🎁 Выбрать подарок','cg:home')],[('← Мои подарки','cg:list:0')]])
            rows=[]
            if payment.status=='gift_created':rows.append([('💳 Оплатить через ЮKassa / СБП',f'cg:pay:{pid}')])
            if d.get('url'):rows.append([('💳 Оплатить', 'url:'+d['url'])])
            rows+=[[('🔄 Проверить оплату',f'cg:check:{pid}')],[('← Мои подарки','cg:list:0')]]
            return styled(f'🎁 <b>Оплата подарка</b>\n\n{devices_label(d["devices"])} · {duration(d["months"])}\nК оплате: {money(payment.amount)}\n\nСсылка получения появится после подтверждения оплаты.',rows)
        if payment.status not in ('balance_paid','order_paid'):return styled('Подарок отменён или платёж возвращён. Ссылка получения недействительна.',[[('🎁 Мои подарки','cg:list:0')]])
        gift=json.loads(await self.repo.setting(gift_key(d['token']),'{}'))
        if gift.get('user'):
            return styled('✅ <b>Подарок получен!</b>\n\nКлюч доступен только получателю. Вы не можете просматривать его подключения или ключ.',[[('🎁 Мои подарки','cg:list:0')],[('🎁 Купить ещё подарок','cg:home')]])
        link=BotGiftLinks(self.repo,self.settings).link(d['token'])
        text=f'🎁 Для вас подарок — VPN Freeman ONE!\n{devices_label(d["devices"])} · {duration(d["months"])}\n\nДля получения откройте бота:\n'+link+'\n\nСрок начнётся при получении.'
        share='url:https://t.me/share/url?'+urlencode(dict(url=link,text=text.split('\n\nДля получения')[0]))
        copy='copy:'+text
        rows=[[('↗️ Отправить другу',share)],[('📋 Скопировать приглашение',copy)],[('🔗 Перевыпустить ссылку',f'cg:rotate:{pid}')],[('🎁 Мои подарки','cg:list:0')],[('💬 Помощь / возврат','support')]]
        return styled(f'🎁 <b>Подарок готов к отправке!</b>\n\n{devices_label(d["devices"])} · {duration(d["months"])}\nОплачено: {money(payment.amount)}\n\nОтправьте приглашение другу. Срок подписки начнётся после получения. Оплаченный подарок не сгорает, пока ждёт активации.',rows,{share:{'style':'primary'},copy:{'style':'success'}})

    async def rotate(self,pid,user):
        await self.lock(user);payment,d=await self.order(pid,user)
        if not d.get('ready'):raise ValueError('Подарок ещё не оплачен.')
        from app.services.vless_creation import durable_session
        oldkey=gift_key(d['token']);newtoken=secrets.token_urlsafe(24)
        async with durable_session(self.repo) as s,s.begin():
            row=await s.get(AppSetting,oldkey,with_for_update=True)
            gift=json.loads(row.value)
            if gift.get('user') or gift['status']!='open':raise ValueError('Подарок уже активируется или получен. Ссылку менять нельзя.')
            row.value=json.dumps({**gift,'status':'revoked'})
            s.add(AppSetting(key=gift_key(newtoken),value=json.dumps(gift)))
            # Rotate order metadata in the same durable transaction as both links.
            order=await s.get(AppSetting,ORDER+str(pid),with_for_update=True)
            frozen=json.loads(order.value);frozen['token']=newtoken;order.value=json.dumps(frozen)
        await self.repo.set_setting(ORDER+str(pid),json.dumps(frozen))
        return await self.receipt(pid,user)
