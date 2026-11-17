"""YooKassa redirects; credit only after authenticated server-side verification."""
import json
import uuid
from decimal import Decimal
from datetime import timedelta,timezone
import aiohttp
from sqlalchemy import select,update,or_
from app.database.models import Payment,User,BalanceTransaction,ChatNotice,now
from app.services.screens import Screen,money


class YooKassa:
    def __init__(self,settings):
        self.settings=settings

    async def request(self,method,path,body=None,key=None):
        headers={'Idempotence-Key':key} if key else {}
        try:
            async with aiohttp.ClientSession(auth=aiohttp.BasicAuth(self.settings.yookassa_shop_id,self.settings.yookassa_secret_key),timeout=aiohttp.ClientTimeout(total=15)) as client:
                async with client.request(method,'https://api.yookassa.ru/v3/payments'+path,json=body,headers=headers) as response:
                    if response.status not in (200,201):
                        try:error=await response.json()
                        except (ValueError,aiohttp.ContentTypeError):error={}
                        if response.status==403 and error.get('code')=='forbidden' and 'recurring' in str(error.get('description','')).lower():
                            raise ValueError('Привязка карты пока недоступна: ЮKassa ещё не подключила автоплатежи для магазина. Обратитесь в поддержку. Обычная оплата подписок доступна; карта не привязана.')
                        raise ValueError('ЮKassa отклонила запрос. Обратитесь в поддержку: код '+str(error.get('code','unknown'))+'. Не повторяйте оплату до проверки её статуса.')
                    return await response.json()
        except (aiohttp.ClientError,TimeoutError):
            raise ValueError('Не удалось связаться с ЮKassa. Повторите проверку позже; повторная проверка не списывает деньги.')

    async def create(self,payment,key,description=None,metadata=None,return_url=None,save_method=False,payment_method=None):
        if payment_method not in (None,'sbp') or (save_method and payment_method):
            raise ValueError('Некорректный способ оплаты.')
        if return_url:
            from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
            target=urlsplit(return_url);base=urlsplit(getattr(self.settings,'web_base_url','https://freemanone.pro'))
            if target.scheme=='https' and target.netloc==base.netloc:
                query=dict(parse_qsl(target.query,keep_blank_values=True));query['payment_id']=str(payment.id)
                return_url=urlunsplit(target._replace(query=urlencode(query)))
        data=dict(freeman_payment=str(payment.id),freeman_user=str(payment.user_id))
        data.update(metadata or {})
        return await self.request('POST','',dict(
            amount=dict(value=f'{payment.amount/100:.2f}',currency='RUB'),capture=True,
            confirmation=dict(type='redirect',return_url=return_url or f'https://t.me/{self.settings.bot_username}'),
            description=description or f'Пополнение баланса F R E E M A N #{payment.id}',metadata=data,**({'save_payment_method':True,'payment_method_data':{'type':'bank_card'}} if save_method else {'payment_method_data':{'type':payment_method}} if payment_method else {})),key)

    async def get(self,id):
        return await self.request('GET','/'+id)

    async def check(self):
        data = await self.request('GET','?limit=1')
        if not isinstance(data,dict) or 'items' not in data:
            raise ValueError('ЮKassa вернула неожиданный ответ.')
        return True


class TopupService:
    def __init__(self,repo,settings,provider=None):
        self.repo,self.settings=repo,settings
        self.provider=provider or YooKassa(settings)

    @property
    def enabled(self):
        return bool(self.settings.yookassa_shop_id and self.settings.yookassa_secret_key)

    async def prepare(self,user,amount):
        if not self.enabled or await self.repo.setting('payment_provider:yookassa','1')!='1':
            return Screen('Самостоятельное пополнение через ЮKassa появится после подключения магазина. Сейчас оплата недоступна.',[[('← Назад','balance')]])
        if user.status=='blocked' or not 100<=amount<=10000000:
            raise ValueError('Сумма должна быть от 1 до 100 000 ₽.')
        payment=await self.repo.add(Payment(user_id=user.id,amount=amount,status='topup_created'))
        await self.repo.set_setting(f'topup_idem:{payment.id}',str(uuid.uuid4()))
        return Screen(f'Пополнить баланс на {money(amount)} через ЮKassa?\nПосле оплаты деньги будут на балансе. Это не покупка подписки.',[[('💳 Перейти к оплате',f'topup_pay:{payment.id}')],[('Отмена','balance')]])

    async def credit(self,payment,data):
        if payment.status=='topup_credited':
            return True
        if data.get('id')!=payment.provider_id:
            raise ValueError('Не совпадает идентификатор платежа.')
        if data.get('status')=='canceled':
            payment.status='topup_canceled';return False
        if data.get('status')!='succeeded' or data.get('paid') is not True:
            return False
        amount=data.get('amount',{});meta=data.get('metadata',{})
        if amount.get('currency')!='RUB' or Decimal(amount.get('value','0'))*100!=payment.amount or meta.get('freeman_payment')!=str(payment.id) or meta.get('freeman_user')!=str(payment.user_id) or bool(data.get('test',False))!=self.settings.yookassa_test_mode:
            raise ValueError('Данные платежа не прошли проверку. Обратитесь в поддержку.')
        claimed=await self.repo.session.execute(update(Payment).where(Payment.id==payment.id,Payment.status=='topup_pending').values(status='topup_processing'))
        if not claimed.rowcount:
            await self.repo.session.refresh(payment)
            return payment.status=='topup_credited'
        from app.services.recurring_cards import remember
        await remember(self.repo,payment,data)
        payment.status='topup_credited'
        await self.repo.add(BalanceTransaction(user_id=payment.user_id,type='yookassa_topup',amount=payment.amount,description=f'Пополнение ЮKassa #{payment.id}'+(' · ТЕСТ' if self.settings.yookassa_test_mode else '')))
        user=await self.repo.session.get(User,payment.user_id)
        if user.telegram_id:
            await self.repo.add(ChatNotice(dedup=f'topup:{payment.id}',target_id=user.telegram_id,kind='client',text=f'✅ Баланс пополнен на {money(payment.amount)}'+(' (тестовый платёж)' if self.settings.yookassa_test_mode else '')+'.'))
        return True

    async def render(self,action,user):
        p=action.split(':')
        if p[0]=='topup':
            return await self.prepare(user,int(p[1]))
        if not self.enabled or await self.repo.setting('payment_provider:yookassa','1')!='1':
            return await self.prepare(user,100)
        await self.repo.session.execute(update(User).where(User.id==user.id).values(status=User.status))
        payment=await self.repo.session.get(Payment,int(p[1]),populate_existing=True)
        if not payment or payment.user_id!=user.id or not payment.status.startswith('topup_'):
            raise ValueError('Платёж недоступен.')
        if payment.status=='topup_credited':
            return Screen('✅ Оплата подтверждена, средства уже на балансе.',[[('💰 Баланс','balance')]])
        if payment.status=='topup_canceled':
            return Screen('Платёж отменён. Средства не зачислены.',[[('Пополнить заново','deposit')]])
        if not payment.provider_id:
            if payment.created_at.replace(tzinfo=timezone.utc)<now()-timedelta(hours=23):
                raise ValueError('Счёт устарел. Создайте новое пополнение.')
            return_url=await self.repo.setting(f'topup_return_url:{payment.id}') or None
            if return_url:
                data=await self.provider.create(payment,await self.repo.setting(f'topup_idem:{payment.id}'),return_url=return_url)
            else:
                data=await self.provider.create(payment,await self.repo.setting(f'topup_idem:{payment.id}'))
            payment.provider_id=data['id'];payment.status='topup_pending'
            url=data.get('confirmation',{}).get('confirmation_url','')
            if not url.startswith('https://'):
                raise ValueError('ЮKassa не вернула ссылку оплаты.')
            await self.repo.set_setting(f'topup_url:{payment.id}',url)
        if p[0]=='topup_check':
            if await self.credit(payment,await self.provider.get(payment.provider_id)):
                return Screen('✅ Средства зачислены на баланс.',[[('💰 Баланс','balance')]])
        url=await self.repo.setting(f'topup_url:{payment.id}')
        return Screen(f'Счёт на {money(payment.amount)}. Оплатите на странице ЮKassa. Бот проверяет платёж автоматически; кнопка проверки нужна только для немедленного обновления.\n\nОбычно зачисление происходит сразу, но обработка банком или ЮKassa может занять до 15 минут. Платёж уже сохранён в истории операций.\n'+('Тестовый режим: настоящие деньги не принимаются.' if self.settings.yookassa_test_mode else ''),[[('💳 Оплатить в ЮKassa / СБП','url:'+url)],[('🔄 Проверить оплату',f'topup_check:{payment.id}')],[('📜 История операций','ledger')],[('← Назад','balance')]])


class SubscriptionCheckoutService:
    """Direct YooKassa checkout that never requires a wallet top-up."""
    def __init__(self,repo,settings,provider=None):
        self.repo,self.settings=repo,settings
        self.provider=provider or YooKassa(settings)

    @property
    def enabled(self):
        return bool(self.settings.yookassa_shop_id and self.settings.yookassa_secret_key)

    async def prepare(self,user,months,sid=0,balance_applied=0,return_url=None,payment_method=None):
        if not self.enabled or await self.repo.setting('payment_provider:yookassa','1')!='1':
            return Screen('Оплата через ЮKassa сейчас недоступна.',[[('← Назад',f'tariff:{months}'+(f':{sid}' if sid else ''))]])
        payment=await self.create_order(user,months,sid,balance_applied,return_url,payment_method)
        return await self.render(f'order_pay:{payment.id}',user)

    async def create_order(self,user,months,sid=0,balance_applied=0,return_url=None,payment_method=None):
        """Stage a local order without contacting the provider; caller commits it."""
        if payment_method not in (None,'sbp'):raise ValueError('Некорректный способ оплаты.')
        if not self.enabled or await self.repo.setting('payment_provider:yookassa','1')!='1':
            raise ValueError('Оплата через ЮKassa сейчас недоступна.')
        tariff=await self.repo.lock_tariff(await self.repo.tariff(months))
        if not tariff or await self.repo.tariff_setting(tariff,'tariff_enabled','1')!='1' or user.status=='blocked':
            raise ValueError('Тариф недоступен.')
        from app.services.billing import purchase_limits
        devices,traffic_limit=await purchase_limits(self.repo,tariff)
        from app.services.pricing import quote
        from app.services.offers import lock_customer,reserve
        await lock_customer(self.repo,user.id)
        total=(await quote(self.repo,user.id,tariff,sid))[0]
        balance_applied=max(0,min(int(balance_applied or 0),total))
        if balance_applied:
            if await self.repo.balance(user.id)<balance_applied:
                raise ValueError('Баланс изменился. Обновите расчёт перед оплатой.')
            await self.repo.add(BalanceTransaction(user_id=user.id,type='checkout_hold',amount=-balance_applied,
                description=f'Резерв на оплату подписки'))
        amount=total-balance_applied
        if amount<100:
            raise ValueError('Остаток к оплате должен быть не менее 1 ₽. Выберите оплату целиком с баланса.')
        if sid:
            from app.services.recurring_cards import unsettled_card
            if await unsettled_card(self.repo,sid):raise ValueError('Платёж автопродления ещё проверяется. Не оплачивайте повторно; обратитесь в поддержку.')
            from app.services.billing import BillingService
            await BillingService(self.repo,self.settings).valid(user,sid)
        payment=await self.repo.add(Payment(user_id=user.id,amount=amount,status='order_created'))
        await reserve(self.repo,payment,tariff,sid,total)
        await self.repo.set_setting(f'order:{payment.id}',json.dumps(dict(months=tariff.months,tariff_id=tariff.id,sid=sid,amount=amount,
            total=total,balance_applied=balance_applied,payment_method=payment_method,return_url=return_url or '',devices=devices,traffic_bytes=traffic_limit)))
        await self.repo.set_setting(f'order_idem:{payment.id}',str(uuid.uuid4()))
        return payment

    async def details(self,payment):
        try: data=json.loads(await self.repo.setting(f'order:{payment.id}','{}'))
        except json.JSONDecodeError: data={}
        if data.get('amount')!=payment.amount or not isinstance(data.get('months'),int) or not 1<=data['months']<=36 or not isinstance(data.get('sid',0),int):
            raise ValueError('Данные заказа повреждены. Обратитесь в поддержку.')
        return data

    async def release_hold(self,payment,order):
        from app.services.offers import release
        await release(self.repo,payment)
        amount=int(order.get('balance_applied') or 0)
        if not amount or await self.repo.setting(f'order_hold_released:{payment.id}')=='1': return
        await self.repo.add(BalanceTransaction(user_id=payment.user_id,type='checkout_release',amount=amount,
            description=f'Возврат резерва по отменённому заказу #{payment.id}'))
        await self.repo.set_setting(f'order_hold_released:{payment.id}','1')

    def verify_confirmation(self,payment,data,order):
        amount=data.get('amount',{});meta=data.get('metadata',{})
        expected={'freeman_payment':str(payment.id),'freeman_user':str(payment.user_id),'freeman_kind':'subscription','freeman_months':str(order['months']),'freeman_sid':str(order['sid'])}
        if (data.get('id')!=payment.provider_id or amount.get('currency')!='RUB'
                or Decimal(amount.get('value','0'))*100!=payment.amount
                or any(meta.get(k)!=v for k,v in expected.items())
                or bool(data.get('test',False))!=self.settings.yookassa_test_mode):
            raise ValueError('Данные платежа не прошли проверку. Обратитесь в поддержку.')

    async def credit(self,payment,data):
        if payment.status=='order_paid': return True
        if data.get('id')!=payment.provider_id: raise ValueError('Не совпадает идентификатор платежа.')
        if data.get('status')=='canceled':
            order=await self.details(payment);payment.status='order_canceled';await self.release_hold(payment,order)
            attempt=await self.repo.setting(f'card_attempt:{payment.id}')
            if attempt:
                from app.services.recurring_cards import failure_notice
                await failure_notice(self.repo,payment,json.loads(attempt))
            return False
        if data.get('status')!='succeeded' or data.get('paid') is not True: return False
        order=await self.details(payment)
        self.verify_confirmation(payment,data,order)
        claimed=await self.repo.session.execute(update(Payment).where(Payment.id==payment.id,Payment.status=='order_pending').values(status='order_processing'))
        if not claimed.rowcount:
            await self.repo.session.refresh(payment)
            return payment.status=='order_paid'
        user=await self.repo.session.get(User,payment.user_id)
        tariff=await self.repo.order_tariff(order)
        from app.services.billing import BillingService
        billing=BillingService(self.repo,self.settings)
        sub,key=await billing.valid(user,order['sid']) if order['sid'] else (None,None)
        sub,key=await billing.fulfill(user,tariff,sub,key,terms=order,operation_key=f"payment:{payment.id}")
        if await self.repo.setting(f'guest_payment:{payment.id}') and key.mode!='vless':
            raise ValueError('Оплата подтверждена, но реальное подключение ещё не готово. Повторите проверку позже; повторно платить не нужно.')
        payment.subscription_id=sub.id;payment.status='order_paid'
        if await self.repo.setting(f'card_attempt:{payment.id}'):
            await self.repo.set_setting(f'web_payment_notice:{user.id}:{payment.id}',json.dumps(dict(text=f'Подписка #{sub.id} автоматически продлена. Оплата {money(payment.amount)} подтверждена.',subscription_id=sub.id,payment_id=payment.id,status='order_paid')))
        if await self.repo.setting(f'guest_payment:{payment.id}') and user.email_verified and user.email:
            from app.services.email_delivery import enqueue,expiry
            await enqueue(self.repo,dedup=f'purchase:{payment.id}',user_id=user.id,subscription_id=sub.id,payment_id=payment.id,kind='purchase',expiry=expiry(sub.expires_at))
        from app.services.offers import consume
        await consume(self.repo,payment,order.get('total',payment.amount))
        from app.web.admin_reports import record_sale
        await record_sale(self.repo,payment,order['months'],sub.device_limit,total=order.get('total',payment.amount),renewal=bool(order.get('sid')),tariff=tariff)
        await self.repo.track_funnel(user.id,'paid',str(order['months']))
        if user.telegram_id:
            await self.repo.add(ChatNotice(dedup=f'order:{payment.id}',target_id=user.telegram_id,kind='client',text=f'✅ Оплата {money(payment.amount)} подтверждена. Подписка #{sub.id} активна.',buttons=json.dumps([[['📦 Открыть подписку',f'sub:{sub.id}']]])))
        return True

    async def success_screen(self, payment, user):
        from app.services.paid_subscription import success_screen
        return await success_screen(self.repo, self.settings, payment, user)

    async def render(self,action,user):
        p=action.split(':');payment=await self.repo.session.get(Payment,int(p[1]),populate_existing=True)
        if not payment or payment.user_id!=user.id or not payment.status.startswith('order_'):
            raise ValueError('Платёж недоступен.')
        if payment.status=='order_paid': return await self.success_screen(payment,user)
        if payment.status=='order_canceled': return Screen('Платёж отменён. Подписка не создана.',[[('💳 Выбрать тариф','buy')]])
        order=await self.details(payment)
        if not payment.provider_id:
            if payment.created_at.replace(tzinfo=timezone.utc)<now()-timedelta(hours=23): raise ValueError('Счёт устарел. Выберите тариф заново.')
            meta=dict(freeman_kind='subscription',freeman_months=str(order['months']),freeman_sid=str(order['sid']))
            method={'payment_method':'sbp'} if order.get('payment_method')=='sbp' else {}
            if order.get('return_url'):
                data=await self.provider.create(payment,await self.repo.setting(f'order_idem:{payment.id}'),f'Подписка F R E E M A N · {order["months"]} мес. #{payment.id}',meta,return_url=order['return_url'],**method)
            else:
                data=await self.provider.create(payment,await self.repo.setting(f'order_idem:{payment.id}'),f'Подписка F R E E M A N · {order["months"]} мес. #{payment.id}',meta,**method)
            payment.provider_id=data['id'];payment.status='order_pending'
            url=data.get('confirmation',{}).get('confirmation_url','')
            if not url.startswith('https://'): raise ValueError('ЮKassa не вернула ссылку оплаты.')
            await self.repo.set_setting(f'order_url:{payment.id}',url)
        if p[0]=='order_check':
            if await self.credit(payment,await self.provider.get(payment.provider_id)):
                return await self.success_screen(payment,user)
            if payment.status=='order_canceled':
                return Screen('Платёж отменён. Подписка не создана, резерв с баланса возвращён.',[[('💳 Выбрать тариф','buy')]])
        url=await self.repo.setting(f'order_url:{payment.id}')
        sbp=order.get('payment_method')=='sbp'
        text=f'💳 <b>К оплате: {money(payment.amount)}</b>\n\n'
        text+=('Нажмите «Оплатить СБП», выберите банк и подтвердите оплату.' if sbp else 'Откройте страницу оплаты и подтвердите платёж.')
        text+='\n\n✅ После оплаты ключ появится автоматически. Нажимать «Проверить оплату» необязательно.\n\nЕсли подтверждение задерживается, повторно не платите: обновите статус кнопкой ниже.'
        ref=f'id{order["tariff_id"]}' if order.get('tariff_id') else str(order['months'])
        return Screen(text,[[('💳 Оплатить СБП' if sbp else '💳 Перейти к оплате','url:'+url)],[('🔄 Проверить оплату',f'order_check:{payment.id}')],[('← Назад',f'tariff:{ref}'+(f':{order["sid"]}' if order['sid'] else ''))]],structured=True)


async def reconcile(database,settings):
    if not settings.yookassa_shop_id or not settings.yookassa_secret_key:
        return
    async with database.sessions() as session:
        items=list(await session.scalars(select(Payment).where(or_(Payment.status.in_(('topup_pending','order_pending','proxy_order_pending','gift_pending')),(Payment.status=='gift_created') & (Payment.created_at<now()-timedelta(hours=23)))).limit(100)))
    provider=YooKassa(settings)
    for item in items:
        try:
            if item.status=='gift_created':
                async with database.sessions.begin() as session:
                    from app.database.repositories import SQLRepository
                    from app.services.customer_gifts import CustomerGifts
                    user=await session.get(User,item.user_id)
                    await CustomerGifts(SQLRepository(session),settings,provider).expire(item.id,user)
                continue
            data=await provider.get(item.provider_id)
            async with database.sessions.begin() as session:
                from app.database.repositories import SQLRepository
                await session.execute(update(User).where(User.id==item.user_id).values(status=User.status))
                payment=await session.get(Payment,item.id,populate_existing=True)
                if payment.status=='topup_pending':
                    await TopupService(SQLRepository(session),settings,provider).credit(payment,data)
                elif payment.status=='gift_pending':
                    from app.services.customer_gifts import CustomerGifts
                    await CustomerGifts(SQLRepository(session),settings,provider).credit(payment,data)
                elif payment.status=='order_pending':
                    await SubscriptionCheckoutService(SQLRepository(session),settings,provider).credit(payment,data)
                elif payment.status=='proxy_order_pending':
                    from app.services.proxies import ProxyCheckoutService
                    await ProxyCheckoutService(SQLRepository(session),settings,provider).credit(payment,data)
        except (ValueError,KeyError):
            continue
