"""Ledger-funded subscriptions. Balance debits and extensions share a transaction."""
import calendar
import json
import secrets
from datetime import timezone, timedelta
from sqlalchemy import select, update
from app.database.models import User, Subscription, AccessKey, Payment, BalanceTransaction, ChatNotice, now
from app.services.screens import Screen, money, duration
from app.services.pricing import quote
from app.services.demo_keys import demo_enabled


def extend(expiry, months):
    current=max(now(),expiry.replace(tzinfo=timezone.utc))
    index=current.month-1+months
    year=current.year+index//12; month=index%12+1
    return current.replace(year=year,month=month,day=min(current.day,calendar.monthrange(year,month)[1]))


async def purchase_limits(repo, tariff, terms=None, *, renewal=False):
    """Validate server-owned allowances before preparing payment or access rows."""
    from app.services.vless_creation import validate_limits

    devices = (terms or {}).get('devices', tariff.device_limit)
    if terms is not None and 'traffic_bytes' in terms:
        traffic = terms['traffic_bytes']
    elif terms is not None and renewal:
        traffic = None  # Historical renewal snapshots do not own a quota change.
    else:
        configured = await repo.setting(f'tariff_traffic:{tariff.id}', '0')
        if not isinstance(configured, str) or not configured.isascii() or not configured.isdecimal() or len(configured) > 19:
            raise ValueError('Некорректный лимит трафика тарифа. Оплата не создана.')
        traffic = int(configured)
    validate_limits(devices, traffic if traffic is not None else 0)
    if terms is not None and 'traffic_bytes' in terms and traffic is None:
        raise ValueError('Некорректные условия трафика оплаты.')
    return devices, traffic


class BillingService:
    def __init__(self,repo,settings):
        self.repo,self.settings=repo,settings

    async def valid(self,user,sid,*,for_update=False):
        if for_update:
            sub=await self.repo.session.scalar(select(Subscription).where(
                Subscription.id==sid,Subscription.user_id==user.id
            ).with_for_update().execution_options(populate_existing=True))
        else:
            sub=await self.repo.subscription(user.id,sid)
        if not sub or user.status=='blocked' or await self.repo.setting(f'subscription_status:{sid}','active')!='active' or await self.repo.setting(f'subscription_removed:{sid}')=='1' or await self.repo.setting(f'subscription_transferred:{sid}')=='1':
            raise ValueError('Подписка недоступна для продления.')
        kid=await self.repo.setting(f'subscription_key:{sid}')
        key=await self.repo.session.get(AccessKey,int(kid),populate_existing=for_update) if kid else None
        blocked=key and key.status=='billing_blocked' and await self.repo.setting(f'billing_blocked:{key.id}')=='1'
        if not key or key.user_id!=user.id or (key.status!='active' and not blocked):
            raise ValueError('Продление этого ключа пока недоступно.')
        if key.mode=='mock' and not demo_enabled(self.repo):
            raise ValueError('Рабочее VPN-подключение не подтверждено. Обратитесь в поддержку.')
        return sub,key

    async def preview(self,user,months,sid):
        from app.services.recurring_cards import unsettled_card
        if sid and await unsettled_card(self.repo,sid):raise ValueError('Платёж автопродления ещё проверяется. Не оплачивайте повторно; обратитесь в поддержку.')
        tariff=await self.repo.lock_tariff(await self.repo.tariff(months))
        if not tariff or await self.repo.tariff_setting(tariff,'tariff_enabled','1')!='1':raise ValueError('Тариф недоступен.')
        amount=(await quote(self.repo,user.id,tariff,sid))[0]
        token=secrets.token_hex(8)
        await self.repo.set_setting('balance_purchase:'+token,json.dumps(dict(actor=user.id,months=tariff.months,tariff_id=tariff.id,sid=sid,amount=amount,expires=now().timestamp()+300)))
        return f'balance_buy:{token}'

    async def fulfill(self,user,tariff,sub=None,key=None,terms=None,*,operation_key=None):
        """Create or extend access after a payment has been authenticated."""
        devices,traffic_limit=await purchase_limits(self.repo,tariff,terms,renewal=sub is not None)
        if sub:
            # Serialize renewals and replace any preloaded ORM snapshot before
            # calculating the expiry, including authenticated provider receipts.
            sub,key=await self.valid(user,sub.id,for_update=True)
            if not self.settings.is_owner(user.telegram_id):
                from app.services.bot_antig_test import profile
                access=await profile(self.repo,sub.id)
                if access and len(access['squads'])==2 and not await self.repo.setting(f'bot_antig_until:{sub.id}'):
                    await self.repo.set_setting(f'bot_antig_until:{sub.id}',sub.expires_at.isoformat())
        expiry=extend(sub.expires_at if sub else now(),tariff.months)
        if not sub:
            sub=await self.repo.add(Subscription(user_id=user.id,tariff_id=tariff.id,kind='paid',expires_at=expiry,device_limit=devices))
            await self.repo.set_setting(f'subscription_traffic_limit:{sub.id}',str(traffic_limit))
            if (terms or {}).get('bot_test_access') is not None:
                from app.services.bot_antig_test import validate_profile
                access=validate_profile(terms['bot_test_access'])
                if str(operation_key or '').startswith('admin-grant:'):
                    if not self.settings.is_owner(user.telegram_id):raise ValueError('Тестовая выдача недоступна.')
                else:
                    from app.services.bot_widget_checkout import verify_paid_profile
                    await verify_paid_profile(self.repo,user,terms,operation_key)
                await self.repo.set_setting(f'bot_test_access:{sub.id}',json.dumps(access))
            from app.services.vless_provision import ensure
            credential=await ensure(self.repo,self.settings,sub,user,operation_key=operation_key)
            if credential:
                key=await self.repo.session.get(AccessKey,credential.access_key_id)
                if not key or (key.mode!='vless' and not demo_enabled(self.repo)):
                    raise ValueError('Панель не подтвердила рабочее VPN-подключение.')
            else:
                if not demo_enabled(self.repo):
                    raise ValueError('Панель не подтвердила рабочее VPN-подключение.')
                key=await self.repo.add(AccessKey(user_id=user.id,name=f'Подписка #{sub.id}',expires_at=expiry))
                await self.repo.set_setting(f'subscription_key:{sub.id}',str(key.id))
                from app.services.demo_keys import create_demo_file
                await create_demo_file(self.repo,key)
        else:
            if key.mode=='vless':
                from app.database.models import VlessCredential
                from app.services.vless_provision import control
                c=await self.repo.session.scalar(select(VlessCredential).where(VlessCredential.subscription_id==sub.id))
                if not c:raise ValueError('Не найдена связь ключа с панелью VLESS.')
                # Authenticated orders own their saved allowance. Historical
                # orders without that field preserve the existing quota instead
                # of inferring purchased terms from today's catalog.
                result=await control(self.repo,self.settings,c,'extend',expiry,renewal=dict(operation_key=operation_key,request=dict(months=tariff.months,tariff_id=tariff.id),devices=devices,traffic_limit=traffic_limit))
                if result and result.replayed:return sub,key
                if result:expiry=result.expires_at
                c.expires_at=expiry;c.device_limit=devices;c.status='active'
            sub.device_limit=devices
            sub.expires_at=expiry;sub.tariff_id=tariff.id;sub.kind='paid';key.expires_at=expiry
            key.status='active'
            await self.repo.set_setting(f'billing_blocked:{key.id}','0')
            if key.mode!='mock':
                from app.services.vpn import VpnService
                credential=await VpnService(self.repo,self.settings).credential(sub.id,user.id)
                if credential:
                    credential.expires_at=expiry
                    await VpnService(self.repo,self.settings).set_subscription_status(sub.id,'active')
        from app.services.subscription_labels import refresh
        from app.services.screens import duration
        await refresh(self.repo,sub,key,duration(tariff.months))
        from app.services.proxy_control import queue_included
        await queue_included(self.repo,sub)
        return sub,key

    async def charge(self,user,tariff,sub,key,amount,reference,automatic=False):
        from app.services.recurring_cards import unsettled_card
        if sub and await unsettled_card(self.repo,sub.id):raise ValueError('Платёж автопродления ещё проверяется. Не оплачивайте повторно; обратитесь в поддержку.')
        from app.services.offers import lock_customer,reserve,consume
        # A completed operation must still replay even after catalogue changes.
        previous=await self.repo.session.scalar(select(Payment).where(Payment.provider_id==reference))
        if previous is None:await purchase_limits(self.repo,tariff)
        await lock_customer(self.repo,user.id)
        previous=await self.repo.session.scalar(select(Payment).where(Payment.provider_id==reference))
        if previous:
            if previous.user_id!=user.id or previous.status!='balance_paid' or previous.amount!=amount or (sub and previous.subscription_id!=sub.id):
                raise ValueError('Этот запрос уже использован для другой оплаты.')
            result=await self.repo.subscription(user.id,previous.subscription_id)
            if not result:raise ValueError('Оплаченная подписка недоступна.')
            return result
        expected=(await quote(self.repo,user.id,tariff,sub.id if sub else 0))[0]
        if amount!=expected:raise ValueError('Стоимость изменилась. Обновите расчёт перед оплатой.')
        source_sid=sub.id if sub else 0
        if amount<0 or await self.repo.balance(user.id)<amount:
            raise ValueError('Недостаточно средств на балансе. Списание не выполнено.')
        was_renewal=sub is not None
        sub,key=await self.fulfill(user,tariff,sub,key,operation_key="balance:"+reference)
        await self.repo.add(BalanceTransaction(user_id=user.id,type='auto_renew' if automatic else 'subscription_purchase',amount=-amount,description=f'{"Автопродление" if automatic else "Подписка"} #{sub.id} · {tariff.months} мес.'))
        payment=await self.repo.add(Payment(user_id=user.id,subscription_id=sub.id,amount=amount,status='balance_paid',provider_id=reference))
        await reserve(self.repo,payment,tariff,source_sid,amount)
        await consume(self.repo,payment)
        from app.web.admin_reports import record_sale
        await record_sale(self.repo,payment,tariff.months,sub.device_limit,renewal=was_renewal,tariff=tariff,source='website' if reference.startswith('web:') else 'unknown')
        await self.repo.track_funnel(user.id,'paid',str(tariff.months))
        return sub

    async def render(self,action,user):
        p=action.split(':')
        if p[0]=='balance_buy':
            await self.repo.session.execute(update(User).where(User.id==user.id).values(status=User.status))
            user=await self.repo.session.get(User,user.id,populate_existing=True)
            data=json.loads(await self.repo.setting('balance_purchase:'+p[1],'{}'))
            if data.get('actor')!=user.id or user.status=='blocked':
                raise ValueError('Оплата недоступна.')
            if data.get('result'):
                sid=data['result']
            else:
                if data.get('expires',0)<now().timestamp():
                    raise ValueError('Подтверждение устарело. Выберите тариф заново.')
                tariff=await self.repo.select_tariff(data['months'],data.get('tariff_id'))
                if not tariff or await self.repo.tariff_setting(tariff, 'tariff_enabled','1')!='1' or (await quote(self.repo,user.id,tariff,data['sid']))[0]!=data['amount']:
                    raise ValueError('Цена изменилась. Выберите тариф заново.')
                sub,key=await self.valid(user,data['sid']) if data['sid'] else (None,None)
                sub=await self.charge(user,tariff,sub,key,data['amount'],'balance:'+p[1])
                sid=sub.id;data['result']=sid
                await self.repo.set_setting('balance_purchase:'+p[1],json.dumps(data))
            from app.services.demo_keys import key_screen
            kid=int(await self.repo.setting(f'subscription_key:{sid}'))
            from app.services.vless_provision import connection
            subscription=await self.repo.subscription(user.id,sid)
            screen=await connection(self.repo,self.settings,subscription,user,paid=True)
            if screen:return screen
            from sqlalchemy import select,func
            from app.database.models import VpnServer
            if await self.repo.session.scalar(select(func.count()).select_from(VpnServer).where(VpnServer.status=='active',VpnServer.health!='offline')):
                from app.services.vpn import VpnService
                return await VpnService(self.repo,self.settings).render(f'v:locations:{sid}',user)
            if await self.repo.setting(f'demo_file:{kid}'):
                return await key_screen(self.repo,self.settings,user,kid,sid,'qr')
            return Screen('✅ Подписка оплачена с баланса.',[[('📦 Подписка',f'sub:{sid}')]])
        sid=int(p[1]);sub=await self.repo.subscription(user.id,sid)
        if not sub:
            raise ValueError('Подписка недоступна.')
        target=f'sub:{sid}'
        if p[0]=='daily':
            if sub.kind not in ('paid','admin'):
                raise ValueError('Ежедневная оплата доступна после покупки подписки.')
            await self.repo.session.execute(update(User).where(User.id==user.id).values(status=User.status))
            await self.valid(user,sid)
            if len(p)>2 and p[2]=='on':
                await self.repo.set_setting(f'auto_renew:{sid}','1')
                await self.repo.set_setting(f'auto_renew_cycle:{sid}','daily')
                await self.repo.set_setting(f'auto_source:{sid}','balance')
            if len(p)>2 and p[2]=='off':
                await self.repo.set_setting(f'auto_renew:{sid}','0')
                await self.repo.set_setting(f'auto_renew_cycle:{sid}','monthly')
            enabled=await self.repo.setting(f'auto_renew:{sid}','0')=='1' and await self.repo.setting(f'auto_renew_cycle:{sid}','monthly')=='daily'
            text=f'📅 <b>Ежедневная оплата · 5 ₽ / 24 часа</b>\n\nСтатус: {"включена" if enabled else "выключена"}\nБаланс: {money(await self.repo.balance(user.id))}\n\nДействует только для этой подписки. После окончания оплаченного срока 5 ₽ списываются с баланса за следующие 24 часа. Сейчас деньги не списываются и купленный срок сохраняется. Месячное автопродление заменяется ежедневным.\n\nЕсли денег недостаточно, доступ блокируется. После пополнения баланс проверяется повторно, оплачиваются следующие 24 часа без списаний за дни простоя. Отключение сохраняет уже оплаченный срок.'
            return Screen(text,[[('Отключить ежедневную оплату' if enabled else '✅ Включить · 5 ₽ в день',f'daily:{sid}:{"off" if enabled else "on"}')],[('← Назад',target)]])
        try:
            await self.valid(user,sid)
        except ValueError:
            if len(p)==2:
                enabled=await self.repo.setting(f'auto_renew:{sid}','0')=='1'
                return Screen('Автопродление приостановлено: ключ не выпущен, заморожен или передаётся.',([[('Отключить автопродление',f'autorenew:{sid}:off')]] if enabled else [])+[[('← Назад',target)]])
            if p[2]!='off':
                raise
        if len(p)>2 and p[2]=='off':
            await self.repo.set_setting(f'auto_renew:{sid}','0')
        if len(p)==2 or (len(p)>2 and p[2]=='off'):
            return await self.render(f'autorenew:{sid}:cards',user)
        if len(p)>2 and p[2] in ('cards','card'):
            import hashlib
            from app.services.recurring_cards import methods
            cards=await methods(self.repo,user.id)
            if p[2]=='cards':
                if not cards:
                    return Screen('💳 <b>Настройка автопродления</b>\n\n<b>Шаг 1. Привяжите карту</b>\n\nДанные карты вводятся на защищённой странице ЮKassa. При привязке 1 ₽ зачисляется на ваш баланс.\n\n<b>Шаг 2. Выберите период</b>\n\nПосле привязки вернитесь сюда и нажмите «Проверить карту». Затем выберите срок продления.\n\nПривязка карты не включает списания автоматически.',[[('Привязать карту через ЮKassa',f'card_bind_info:{sid}')],[('Проверить карту',f'autorenew:{sid}')],[('Управление автоплатежами','payment_management')],[('← Назад',target)]])
                rows=[]
                for card in cards:
                    token=hashlib.sha256(card['id'].encode()).hexdigest()[:16]
                    for tariff in await self.repo.tariffs():
                        if tariff.device_limit==sub.device_limit and await self.repo.tariff_setting(tariff, 'tariff_enabled','1')=='1':
                            amount=(await quote(self.repo,user.id,tariff,sid))[0]
                            rows.append([(f"{card.get('brand','Карта')} • {card.get('last4','')} · {duration(tariff.months)} · {money(amount)}",f'autorenew:{sid}:card:id{tariff.id}:{token}')])
                enabled=await self.repo.setting(f'auto_renew:{sid}','0')=='1'
                if enabled:rows.append([('Отключить автопродление',f'autorenew:{sid}:off')])
                state='Включено' if enabled else 'Выключено'
                return Screen(f'🔄 <b>Автопродление: {state}</b>\n\nКарта привязана.\n\n<b>Выберите период продления</b>\n\nНажатие разрешает списание актуальной стоимости выбранного тарифа в день окончания подписки.\n\nОтключить автопродление или отвязать карту можно в разделе «Помощь → Автоплатежи».',rows+[[('Управление автоплатежами','payment_management')],[('← Назад',target)]])
            await self.valid(user,sid)
            card=next((c for c in cards if hashlib.sha256(c['id'].encode()).hexdigest()[:16]==p[4]),None)
            tariff=await self.repo.tariff(p[3]);months=tariff.months if tariff else 0
            if not card or not tariff or tariff.device_limit!=sub.device_limit or await self.repo.tariff_setting(tariff,'tariff_enabled','1')!='1':raise ValueError('Способ оплаты или тариф недоступен.')
            if not self.settings.yookassa_shop_id or not self.settings.yookassa_secret_key or await self.repo.setting('payment_provider:yookassa','1')!='1':raise ValueError('Оплата картой временно недоступна.')
            await self.repo.set_setting(f'auto_card:{sid}',card['id'])
            await self.repo.set_setting(f'auto_source:{sid}','card')
            await self.repo.set_setting(f'auto_renew_months:{sid}',str(months))
            await self.repo.set_setting(f'auto_renew_tariff:{sid}',str(tariff.id))
            await self.repo.set_setting(f'auto_renew:{sid}','1')
            await self.repo.set_setting(f'auto_renew_cycle:{sid}','monthly')
        if len(p)>2 and p[2]=='card':return await self.render(f'autorenew:{sid}:cards',user)
        if len(p)>2 and p[2]=='on':
            await self.valid(user,sid)
            tariff=await self.repo.tariff(p[3]);months=tariff.months if tariff else 0
            if not tariff or await self.repo.tariff_setting(tariff,'tariff_enabled','1')!='1':
                raise ValueError('Тариф недоступен.')
            await self.repo.set_setting(f'auto_source:{sid}','balance')
            await self.repo.set_setting(f'auto_renew_months:{sid}',str(months))
            await self.repo.set_setting(f'auto_renew_tariff:{sid}',str(tariff.id))
            await self.repo.set_setting(f'auto_renew:{sid}','1')
            await self.repo.set_setting(f'auto_renew_cycle:{sid}','monthly')
        enabled=await self.repo.setting(f'auto_renew:{sid}','0')=='1'
        rows=[]
        daily=await self.repo.setting(f'auto_renew_cycle:{sid}','monthly')=='daily'
        if enabled and daily:
            text='📅 Ежедневная оплата включена · 5 ₽ / 24 часа'
            rows.append([('Настроить ежедневную оплату',f'daily:{sid}')])
            rows.append([('Отключить автопродление',f'autorenew:{sid}:off')])
        elif enabled:
            months=int(await self.repo.setting(f'auto_renew_months:{sid}','1'))
            tariff=await self.repo.auto_tariff(sid)
            amount=(await quote(self.repo,user.id,tariff,sid))[0] if tariff else 0
            text=f'🔄 Автопродление включено\nПериод: {duration(months)}\nТекущая цена: {money(amount)}'
            rows.append([('Отключить автопродление',f'autorenew:{sid}:off')])
        else:
            text='🔄 Автопродление выключено\n\nВыберите период, чтобы включить автопродление этой подписки:'
            for tariff in await self.repo.tariffs():
                if tariff.device_limit==sub.device_limit and await self.repo.tariff_setting(tariff, 'tariff_enabled','1')=='1':
                    amount=(await quote(self.repo,user.id,tariff,sid))[0]
                    rows.append([(f'Включить · {duration(tariff.months)} · {money(amount)}',f'autorenew:{sid}:on:id{tariff.id}')])
        source=await self.repo.setting(f'auto_source:{sid}','balance')
        master=await self.repo.setting(f'auto_master:{user.id}','1')=='1'
        if not master:text+='\nОбщий режим выключен: автоматические списания приостановлены.'
        text+=('\n\nИсточник: сохранённая карта. Управляйте картами в разделе «Баланс → Платежи и автопродление».\nСписание в день окончания по актуальной цене выбранного тарифа. При отказе банка подписка не продлевается.' if source=='card' else f'\n\nБаланс: {money(await self.repo.balance(user.id))}\nИсточник: внутренний баланс. Списание в день окончания по актуальной цене. При недостатке средств продления нет.')
        if enabled:
            for tariff in await self.repo.tariffs():
                if tariff.device_limit==sub.device_limit and await self.repo.tariff_setting(tariff, 'tariff_enabled','1')=='1':
                    amount=(await quote(self.repo,user.id,tariff,sid))[0]
                    rows.append([(f'С баланса · {duration(tariff.months)} · {money(amount)}',f'autorenew:{sid}:on:id{tariff.id}')])
        rows.append([('Выбрать сохранённую карту',f'autorenew:{sid}:cards')])
        rows.append([('Добавить карту через ЮKassa',f'card_bind_info:{sid}')])
        if sub.kind in ('paid','admin') and not (enabled and daily):
            rows.append([('📅 Ежедневно · 5 ₽',f'daily:{sid}')])
        return Screen(text,rows+[[('💰 Баланс','balance')],[('← Назад',target)]])


async def prepare_autorenew(repo, settings=None):
    from app.config.settings import Settings
    service=BillingService(repo,settings or Settings(_env_file=None))
    subs=list(await repo.session.scalars(select(Subscription).where(Subscription.expires_at<=now())))
    for sub in subs:
        if await repo.setting(f'auto_renew:{sub.id}','0')!='1':
            continue
        if await repo.setting(f'auto_source:{sub.id}','balance')=='card':
            continue
        from app.services.recurring_cards import unsettled_card
        if await unsettled_card(repo,sub.id):continue
        user=await repo.session.get(User,sub.user_id)
        if not user or await repo.setting(f'auto_master:{user.id}','1')!='1':
            continue
        # Serialize balance debits with purchases and withdrawals for this
        # customer only. Re-read state after waiting for a competing renewal.
        await repo.session.execute(update(User).where(User.id==user.id).values(status=User.status))
        await repo.session.refresh(user)
        await repo.session.refresh(sub,with_for_update=True)
        if sub.expires_at.replace(tzinfo=timezone.utc)>now():
            continue
        if await repo.setting(f'auto_renew:{sub.id}','0')!='1' or await repo.setting(f'auto_master:{user.id}','1')!='1':
            continue
        if await repo.setting(f'auto_source:{sub.id}','balance')=='card' or await unsettled_card(repo,sub.id):
            continue
        try:
            _,key=await service.valid(user,sub.id)
        except ValueError:
            continue
        if await repo.setting(f'auto_renew_cycle:{sub.id}','monthly')=='daily':
            if sub.kind not in ('paid','admin'):
                continue
            reference=f'daily:{sub.id}:{sub.expires_at.isoformat()}'
            if await repo.session.scalar(select(Payment.id).where(Payment.provider_id==reference)):
                continue
            if await repo.balance(user.id)<500:
                key.status='billing_blocked'
                await repo.set_setting(f'billing_blocked:{key.id}','1')
                if key.mode!='mock':
                    from app.services.vpn import VpnService
                    await VpnService(repo,service.settings).set_subscription_status(sub.id,'disabled')
                dedup=reference+':insufficient'
                if not await repo.session.scalar(select(ChatNotice.id).where(ChatNotice.dedup==dedup)):
                    if user.telegram_id:
                        await repo.add(ChatNotice(dedup=dedup,target_id=user.telegram_id,kind='client',text=f'⚠️ Подписка #{sub.id} закончилась. Для ежедневного продления требуется 5 ₽ на балансе. Доступ приостановлен до пополнения.'))
            else:
                expiry=max(now(),sub.expires_at.replace(tzinfo=timezone.utc))+timedelta(days=1)
                if key.mode=='vless':
                    from app.database.models import VlessCredential
                    from app.services.vless_provision import control
                    credential=await repo.session.scalar(select(VlessCredential).where(VlessCredential.subscription_id==sub.id))
                    if not credential:raise ValueError('Не найдена связь ключа с панелью VLESS.')
                    result=await control(repo,service.settings,credential,'extend',expiry,renewal=dict(operation_key=reference,request=dict(days=1),devices=sub.device_limit))
                    if result:expiry=result.expires_at
                sub.expires_at=expiry;key.expires_at=expiry;key.status='active'
                await repo.set_setting(f'billing_blocked:{key.id}','0')
                if key.mode not in ('mock','vless'):
                    from app.services.vpn import VpnService
                    vpn=VpnService(repo,service.settings);credential=await vpn.credential(sub.id,user.id)
                    if credential:
                        credential.expires_at=expiry
                    await vpn.set_subscription_status(sub.id,'active')
                await repo.add(BalanceTransaction(user_id=user.id,type='daily_renew',amount=-500,description=f'Подписка #{sub.id} · 24 часа'))
                payment=await repo.add(Payment(user_id=user.id,subscription_id=sub.id,amount=500,status='balance_paid',provider_id=reference))
                from app.web.admin_reports import record_sale
                await record_sale(repo,payment,0,sub.device_limit,renewal=True)
            continue
        months=int(await repo.setting(f'auto_renew_months:{sub.id}','1'))
        tariff=await repo.auto_tariff(sub.id)
        if not tariff or await repo.tariff_setting(tariff,'tariff_enabled','1')!='1':
            continue
        amount=(await quote(repo,user.id,tariff,sub.id))[0]
        reference=f'auto:{sub.id}:{sub.expires_at.isoformat()}'
        if await repo.session.scalar(select(Payment.id).where(Payment.provider_id==reference)):
            continue
        insufficient=await repo.balance(user.id)<amount
        if not insufficient:
            await service.charge(user,tariff,sub,key,amount,reference,True)
        dedup=reference+(':insufficient' if insufficient else ':paid')
        if not await repo.session.scalar(select(ChatNotice.id).where(ChatNotice.dedup==dedup)):
            text=f'⚠️ Не хватает {money(amount)} на продление подписки #{sub.id}. Списание не выполнено.' if insufficient else f'✅ Подписка #{sub.id} продлена до {sub.expires_at:%d.%m.%Y}. С баланса списано {money(amount)}.'
            if user.telegram_id:
                await repo.add(ChatNotice(dedup=dedup,target_id=user.telegram_id,kind='client',text=text))
