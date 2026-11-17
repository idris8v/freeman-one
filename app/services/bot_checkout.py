"""One bot checkout screen; provider creation happens only on the payment link."""
import hashlib
import json
import secrets
from datetime import timedelta,timezone
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from app.database.models import Payment,User,Tariff,now
from app.services.offers import lock_customer
from app.services.screens import Screen,money,duration
from app.services.topups import SubscriptionCheckoutService

BANK_WAIT = ('⏳ После оплаты дождитесь подтверждения банка. '
             'Иногда ответ приходит с задержкой. Если деньги уже списались, повторно платить не нужно.')

def link_key(token):
    return 'bot_checkout_link:'+hashlib.sha256(token.encode()).hexdigest()

class BotCheckout:
    def __init__(self,repo,settings,provider=None):
        self.repo,self.settings=repo,settings
        self.service=SubscriptionCheckoutService(repo,settings,provider)

    async def lock_tariff(self,tid):
        # Other checkout paths may own a tariff before waiting for this user.
        # Do not wait in the reverse order: a savepoint + NOWAIT avoids a cycle.
        try:
            async with self.repo.session.begin_nested():
                return await self.repo.session.scalar(select(Tariff).where(Tariff.id==tid).with_for_update(nowait=True).execution_options(populate_existing=True))
        except DBAPIError as error:
            if getattr(error.orig,'sqlstate',None)=='55P03':
                raise ValueError('Тариф сейчас обновляется. Повторите переход к оплате через несколько секунд.') from error
            raise

    async def owned(self,pid,user):
        if not 0<pid<2**63:raise ValueError('Заказ недоступен.')
        await lock_customer(self.repo,user.id)
        user=await self.repo.session.get(User,user.id,populate_existing=True)
        payment=await self.repo.session.scalar(select(Payment).where(Payment.id==pid,Payment.user_id==user.id).with_for_update().execution_options(populate_existing=True))
        if user.status=='blocked' or not payment or not payment.status.startswith('order_'):
            raise ValueError('Заказ недоступен.')
        if await self.repo.setting(f'bot_checkout:{payment.id}')!='1':
            raise ValueError('Заказ недоступен.')
        if (payment.status=='order_created' and not payment.provider_id
            and not await self.repo.setting(f'bot_checkout_started:{payment.id}')
            and await self.repo.setting(f'widget_order:{payment.id}')):
            order=await self.service.details(payment)
            if len((order.get('bot_test_access') or {}).get('squads',[]))==2:
                from app.services.bot_antig_price import require_enabled
                await require_enabled(self.repo)
        return payment

    async def prepare(self,user,ref,sid=0):
        if not self.service.enabled or await self.repo.setting('payment_provider:yookassa','1')!='1':
            raise ValueError('Оплата сейчас недоступна.')
        await lock_customer(self.repo,user.id)
        tariff=await self.repo.tariff(ref)
        if not tariff or await self.repo.tariff_setting(tariff,'tariff_enabled','1')!='1':
            raise ValueError('Тариф недоступен.')
        pointer=f'bot_checkout_current:{user.id}:{tariff.id}:{sid}'
        previous=await self.repo.setting(pointer)
        payment=await self.owned(int(previous),user) if previous else None
        # Changing a tariff abandons only invoices never handed to the provider.
        # Release their personal-offer reservation before quoting the new choice.
        abandoned=list(await self.repo.session.scalars(select(Payment).where(Payment.user_id==user.id,Payment.status=='order_created',Payment.provider_id.is_(None))))
        for old in abandoned:
            if payment and old.id==payment.id:continue
            if await self.repo.setting(f'bot_checkout:{old.id}')=='1' and not await self.repo.setting(f'bot_checkout_started:{old.id}'):
                await self.service.release_hold(old,await self.service.details(old))
                old.status='order_canceled'
        if payment and payment.status=='order_created' and not await self.repo.setting(f'bot_checkout_started:{payment.id}') and payment.created_at.replace(tzinfo=timezone.utc)<now()-timedelta(hours=1):
            await self.service.release_hold(payment,await self.service.details(payment))
            payment.status='order_canceled'
        if not payment or payment.status not in ('order_created','order_pending','order_processing'):
            await self.lock_tariff(tariff.id)
            payment=await self.service.create_order(user,ref,sid,payment_method='sbp')
            await self.repo.set_setting(f'bot_checkout:{payment.id}','1')
            await self.repo.set_setting(pointer,str(payment.id))
        return await self.screen(payment,user)

    async def screen(self,payment,user,note=''):
        if payment.status=='order_paid':return await self.service.success_screen(payment,user)
        order=await self.service.details(payment)
        ref=f'id{order["tariff_id"]}'
        back=f'tariff:{ref}'+(f':{order["sid"]}' if order['sid'] else '')
        if payment.status=='order_canceled':
            return Screen('Счёт закрыт. Чтобы выбрать способ оплаты заново, вернитесь к тарифу.',[[('← Назад',back)]],structured=True)
        raw=await self.repo.setting(f'bot_checkout_token:{payment.id}')
        info=json.loads(raw) if raw else {}
        if info.get('expires',0)<now().timestamp()+60:
            info=dict(token=secrets.token_urlsafe(32),expires=now().timestamp()+3600)
            await self.repo.set_setting(f'bot_checkout_token:{payment.id}',json.dumps(info))
            await self.repo.set_setting(link_key(info['token']),json.dumps(dict(payment_id=payment.id,user_id=user.id,expires=info['expires'])))
        url=self.settings.web_base_url.rstrip('/')+'/pay/bot/'+info['token']
        available=await self.repo.balance(user.id)
        balance=money(available)
        from app.services.bot_catalog import devices_label
        result='текущий ключ продлится автоматически' if order['sid'] else 'ключ появится автоматически'
        text=f'💳 <b>Оплата подписки</b>\n\n{devices_label(order["devices"])} · {duration(order["months"])}\n\n<b>К оплате: {money(payment.amount)}</b>\nНа балансе: {balance}\n\n{BANK_WAIT}\n\n✅ После подтверждения оплаты {result}.'
        started = bool(payment.provider_id or await self.repo.setting(f'bot_checkout_started:{payment.id}'))
        if not note and started:
            note = '⏳ Ожидаем подтверждение банка. Проверьте статус этого счёта после оплаты.'
        if note:
            text += '\n\n' + note
        rows=[[('💳 Оплатить СБП','url:'+url)]]
        if (available >= payment.amount and payment.status == 'order_created' and not payment.provider_id
                and not await self.repo.setting(f'bot_checkout_started:{payment.id}')):
            rows.append([('💰 Оплатить с баланса',f'checkout_balance:{payment.id}')])
        rows.extend([[('🔄 Проверить оплату',f'checkout_check:{payment.id}')],[('← Назад',back)]])
        return Screen(text,rows,structured=True,appearance_page='pay')

    async def render(self,action,user):
        p=action.split(':')
        if p[0]=='checkout':return await self.prepare(user,p[1],int(p[2]) if len(p)>2 else 0)
        payment=await self.owned(int(p[1]),user)
        if await self.repo.setting(f'widget_order:{payment.id}'):
            from app.services.bot_widget_checkout import payment_screen
            return await payment_screen(self,payment,user,action=action)
        if p[0]=='checkout_view':return await self.screen(payment,user)
        if payment.status=='order_paid':return await self.service.success_screen(payment,user)
        if p[0]=='checkout_check':
            if payment.provider_id:
                if await self.service.credit(payment,await self.service.provider.get(payment.provider_id)):
                    return await self.service.success_screen(payment,user)
                note='Платёж пока не подтверждён. Если вы уже оплатили, не платите повторно.'
            elif await self.repo.setting(f'bot_checkout_started:{payment.id}'):
                note='Ссылка СБП уже открывалась. Если платёжная страница не загрузилась, откройте СБП ещё раз: будет использован тот же счёт.'
            else:note='Оплата ещё не начата. Выберите СБП или оплату с баланса.'
            return await self.screen(payment,user,note)
        if p[0]!='checkout_balance':raise ValueError('Действие недоступно.')
        order=await self.service.details(payment)
        raw=await self.repo.setting(f'bot_checkout_balance:{payment.id}')
        choice=json.loads(raw) if raw else {}
        if choice:
            data=json.loads(await self.repo.setting('balance_purchase:'+choice['target'].split(':')[1],'{}'))
            if data.get('result'):
                from app.services.billing import BillingService
                return await BillingService(self.repo,self.settings).render(choice['target'],user)
        if not payment.provider_id and await self.repo.setting(f'bot_checkout_started:{payment.id}'):
            return await self.screen(payment,user,'Оплата СБП уже начата. Откройте СБП повторно для восстановления того же счёта. Менять способ оплаты до проверки результата нельзя.')
        # Serialize against the web handoff. Never debit the wallet while a
        # provider payment could still succeed for this same checkout.
        if payment.provider_id and payment.status!='order_canceled':
            if await self.service.credit(payment,await self.service.provider.get(payment.provider_id)):
                return await self.service.success_screen(payment,user)
            if payment.status!='order_canceled':
                return await self.screen(payment,user,'Оплата СБП уже открыта. Завершите её или дождитесь отмены платежа, прежде чем менять способ оплаты.')
        if await self.repo.balance(user.id)<payment.amount:
            screen=await self.screen(payment,user,'На балансе недостаточно средств. Оплатите через СБП или пополните баланс.')
            screen.rows.insert(-1,[('➕ Пополнить баланс',f'balance~c{payment.id}')])
            return screen
        await self.service.release_hold(payment,order)
        payment.status='order_canceled'
        from app.services.billing import BillingService
        ref=f'id{order["tariff_id"]}'
        await self.lock_tariff(order['tariff_id'])
        from app.services.pricing import quote
        amount=(await quote(self.repo,user.id,await self.repo.tariff(ref),order['sid']))[0]
        if choice.get('expires',0)>now().timestamp() and choice.get('amount')==amount:
            target=choice['target']
        else:
            target=await BillingService(self.repo,self.settings).preview(user,ref,order['sid'])
            await self.repo.set_setting(f'bot_checkout_balance:{payment.id}',json.dumps(dict(target=target,amount=amount,expires=now().timestamp()+300)))
        return Screen(f'💰 <b>Оплата с баланса</b>\n\nК списанию: <b>{money(amount)}</b>\nДоступно: {money(await self.repo.balance(user.id))}\n\nПодтвердите оплату. Ссылка СБП для этого счёта закрыта.',[[('✅ Подтвердить оплату',target)],[('← Назад',f'tariff:{ref}'+(f':{order["sid"]}' if order['sid'] else ''))]],structured=True)
