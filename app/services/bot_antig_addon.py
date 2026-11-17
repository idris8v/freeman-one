"""Bot-only paid additions to an existing key; verified remote operation owns recovery."""
import json,secrets,re
from datetime import datetime,timedelta
from decimal import Decimal,ROUND_CEILING
from sqlalchemy import select
from app.database.models import User,Subscription,AccessKey,VlessCredential,RemnawaveServer,Payment,BalanceTransaction,AppSetting,VlessRenewal,now
from app.services.admin_client_grant import rich
from app.services.vless_creation import utc
from app.services.bot_antig_price import enabled,monthly,tariff_total
from app.services.bot_antig_test import profile,validate_profile
from app.services.screens import money,duration

PREFIX='bot_antig_addon:'

async def deadline(repo,sub):
    raw=await repo.setting(f'bot_antig_until:{sub.id}')
    return utc(datetime.fromisoformat(raw)) if raw else utc(sub.expires_at)

async def has_access(repo,sub):
    p=await profile(repo,sub.id)
    return bool(p and len(p['squads'])==2 and await deadline(repo,sub)>now())

async def access(repo,settings,c):
    from app.services.remnawave import RemnawaveService
    panel=await repo.session.get(RemnawaveServer,c.server_id)
    if not panel or panel.status!='active' or not panel.internal_squad_uuid:raise ValueError('Панель ключа недоступна.')
    api=RemnawaveService(repo,settings).api(panel)
    raw=await api.squads();raw=raw.get('response',raw);items=raw.get('internalSquads',raw.get('items',[])) if isinstance(raw,dict) else raw
    anti=[x for x in items if str(x.get('name','')).casefold().replace('-','').replace(' ','')=='antig' and x.get('inbounds')]
    if len(anti)!=1:raise ValueError('На панели этого ключа антиглушилка пока не настроена.')
    desired=validate_profile(dict(panel=panel.id,squads=[panel.internal_squad_uuid,anti[0]['uuid']]))
    raw=await api.user(c.remote_user_uuid);raw=raw.get('response',raw)
    groups=raw.get('activeInternalSquads')
    if not isinstance(groups,list):raise ValueError('Панель не подтвердила текущий набор серверов.')
    base=sorted({x.get('uuid') if isinstance(x,dict) else x for x in groups})
    if panel.internal_squad_uuid not in base:raise ValueError('Исходный сквад ключа изменён. Нужна проверка администратора.')
    return desired,base

async def rate_remaining(repo,sub,at):
    if await has_access(repo,sub):at=max(at,await deadline(repo,sub))
    seconds=max(0,(utc(sub.expires_at)-at).total_seconds())
    if not seconds:return 0
    tariff=await repo.tariff_by_id(sub.tariff_id) if sub.tariff_id else None
    if not tariff:
        data=json.loads(await repo.setting(f'bot_antig_custom_price:{sub.id}','{}'))
        if data.get('expiry')!=utc(sub.expires_at).isoformat() or data.get('devices')!=sub.device_limit or type(data.get('amount')) is not int:
            raise ValueError('Для индивидуальной подписки администратору нужно назначить цену антиглушилки.')
        return data['amount']
    return int((Decimal(await tariff_total(repo,tariff))*Decimal(str(seconds))/Decimal(tariff.months*30*86400)).quantize(Decimal(1),rounding=ROUND_CEILING))

class Addon:
    def __init__(self,repo,settings):self.repo,self.settings=repo,settings

    async def context(self,user,sid):
        if user.status!='active':raise PermissionError('Подписка недоступна.')
        from app.services.billing import BillingService
        sub,key=await BillingService(self.repo,self.settings).valid(user,sid)
        c=await self.repo.session.scalar(select(VlessCredential).where(VlessCredential.subscription_id==sid,VlessCredential.user_id==user.id))
        if not c or c.status=='deleted':raise ValueError('Рабочий ключ не найден.')
        return sub,key,c

    async def saved(self,user,token):
        if not re.fullmatch('[0-9a-f]{20}',token):raise ValueError('Расчёт недоступен.')
        raw=await self.repo.session.get(AppSetting,PREFIX+token,populate_existing=True)
        d=json.loads(raw.value) if raw else {}
        if d.get('user')!=user.id:raise PermissionError('Расчёт другого клиента.')
        return d

    async def save(self,token,d):await self.repo.set_setting(PREFIX+token,json.dumps(d))

    async def quote(self,user,sid,tid=0):
        partner=self.settings.role(user.telegram_id)=='partner'
        if partner:
            from app.services.partner_bot import PartnerService
            await PartnerService(self.repo,self.settings).authorize(user)
            if tid:raise ValueError('Откройте антиглушилку из карточки нужного ключа.')
        sub,key,c=await self.context(user,sid)
        if not self.settings.is_owner(user.telegram_id) and not await enabled(self.repo):raise ValueError('Антиглушилка сейчас недоступна для подключения.')
        if not tid and await has_access(self.repo,sub) and await deadline(self.repo,sub)>=utc(sub.expires_at):return rich('🛡 Антиглушилка уже подключена до окончания этой подписки.',[[('← Ключ',f'sub:{sid}')]])
        if not tid and utc(sub.expires_at)<=now():raise ValueError('Сначала продлите подписку.')
        pending=await self.repo.setting(f'bot_antig_open:{user.id}:{sid}')
        if pending:
            d=await self.saved(user,pending)
            if d.get('held') and not d.get('done'):return await self.summary(user,pending,d)
        from app.services.billing import extend,purchase_limits
        tariff=await self.repo.tariff_by_id(tid) if tid else None
        base=0;new_extra=0;devices=sub.device_limit;traffic=None;months=0;end=utc(sub.expires_at)
        if tid:
            from app.services.bot_catalog import available
            if not any(t.id==tid for t in await available(self.repo)):raise ValueError('Тариф недоступен.')
            from app.services.pricing import quote
            base=(await quote(self.repo,user.id,tariff,sid))[0]
            new_extra=await tariff_total(self.repo,tariff);devices,traffic=await purchase_limits(self.repo,tariff)
            months=tariff.months;end=extend(sub.expires_at,months)
        plan=json.loads(await self.repo.setting(f'partner_plan:{sub.id}','{}')) if partner and not sub.tariff_id else {}
        if plan:
            from app.services.partner_issuance import addon_price
            seconds=max(0,(utc(sub.expires_at)-now()).total_seconds())
            price=await addon_price(self.repo,plan['months'])
            rest=int((Decimal(price)*Decimal(str(seconds))/Decimal(plan['months']*30*86400)).quantize(Decimal(1),rounding=ROUND_CEILING))
        else:
            rest=await rate_remaining(self.repo,sub,now())
            if partner:rest=(rest+1)//2
        profile,groups=await access(self.repo,self.settings,c)
        total=base+rest+new_extra
        if not 0<=total<=100000000:raise ValueError('Сумма слишком велика.')
        token=secrets.token_hex(10)
        d=dict(user=user.id,sid=sid,cid=c.id,remote=c.remote_user_uuid,base_expiry=utc(sub.expires_at).isoformat(),expiry=utc(end).isoformat(),base_devices=sub.device_limit,devices=devices,traffic=traffic,tariff=tid,months=months,base=base,rest=rest,extra=new_extra,amount=total,profile=profile,groups=groups,until=now().timestamp()+600,held=False,done=False,partner=partner)
        await self.save(token,d)
        return await self.summary(user,token,d)

    async def summary(self,user,token,d):
        if d.get('partner'):
            from app.services.partner_ledger import entries,total
            amount=money(d['amount']);sid=d['sid']
            if d.get('done'):
                debt=money(total(await entries(self.repo,user.id)))
                return rich(f'✅ <b>Антиглушилка подключена</b>\n\nНачислено {amount}\nТекущий долг <b>{debt}</b>',
                            [[('💼 Расчёты','partner:ledger')],[('← Ключ',f'sub:{sid}')]])
            text=f'🛡 <b>Антиглушилка для ключа №{sid}</b>\n\nДоплата за оставшийся срок <b>{amount}</b>. Сумма запишется в долг после подключения.'
            if d.get('held'):
                rows=[[('Проверить подключение',f'ag:apply:{token}')]]
            elif d['until']<now().timestamp():
                rows=[[('Обновить расчёт',f'ag:quote:{sid}')]]
            else:
                rows=[[('Подключить и записать в долг',f'ag:apply:{token}')]]
            return rich(text,rows+[[('← Ключ',f'sub:{sid}')]])
        if d.get('done'):return rich('✅ Антиглушилка подключена. Ссылка ключа сохранена.',[[('🔑 Открыть ключ',f'sub:{d["sid"]}')]])
        text='🛡 <b>Антиглушилка</b>\n\nСервер добавится в этот же ключ.\nДо: '+datetime.fromisoformat(d['expiry']).strftime('%d.%m.%Y')
        if d['tariff']:text+='\n\nПродление VPN: '+money(d['base'])+'\nДополнение на новый срок: '+money(d['extra'])
        text+='\nДополнение за остаток: '+money(d['rest'])+'\n\n<b>Итого: '+money(d['amount'])+'</b>'
        if d.get('held'):
            text+='\n\n⏳ Панель проверяет изменение. Сумма зарезервирована; повторная оплата не требуется.'
            rows=[[('🔄 Проверить подключение',f'ag:{d.get("mode") if d.get("mode") in ("sale","free") else "apply"}:{token}')]]
        elif d['until']<now().timestamp():
            rows=[[('🔄 Обновить расчёт',f'ag:quote:{d["sid"]}:{d["tariff"]}')]]
        elif self.settings.is_owner(user.telegram_id):
            rows=[[('✅ Подключить и учесть',f'ag:sale:{token}')],[('🎁 Подключить бесплатно',f'ag:free:{token}')]]
        elif await self.repo.balance(user.id)>=d['amount']:
            rows=[[('✅ Подключить · '+money(d['amount']),f'ag:apply:{token}')]]
        else:
            rows=[[('💳 Пополнить недостающее',f'ag:fund:{token}')],[('🔄 Проверить баланс',f'ag:show:{token}')]]
        if not d.get('held'):rows.append([('← Ключ',f'sub:{d["sid"]}')])
        return rich(text,rows)

    async def apply(self,user,token,d,mode='wallet'):
        from app.services.offers import lock_customer
        from app.services.vless_renewal import operation_id
        await lock_customer(self.repo,user.id);d=await self.saved(user,token)
        partner=self.settings.role(user.telegram_id)=='partner'
        if partner:
            from app.services.partner_bot import PartnerService
            await PartnerService(self.repo,self.settings).authorize(user)
        if partner!=bool(d.get('partner')) or partner!=(mode=='partner'):
            raise PermissionError('Откройте новый расчёт с действующей ролью.')
        if d.get('done'):return await self.summary(user,token,d)
        if mode not in ('wallet','partner') and not self.settings.is_owner(user.telegram_id):raise PermissionError('Действие доступно администратору.')
        if d.get('mode') not in (None,mode):raise ValueError('Сначала завершите исходный режим выдачи.')
        sub,key,c=await self.context(user,d['sid'])
        ref='bot-antig:'+token
        op=await self.repo.session.get(VlessRenewal,operation_id(user.id,ref),populate_existing=True)
        recovering=op and op.state!='failed'
        if not recovering:
            if partner and await has_access(self.repo,sub) and await deadline(self.repo,sub)>=utc(sub.expires_at):
                return rich('Антиглушилка уже подключена до окончания ключа.',
                            [[('← Ключ',f'sub:{sub.id}')]])
            if d['until']<now().timestamp():raise ValueError('Расчёт устарел. Откройте новый расчёт.')
            if utc(sub.expires_at).isoformat()!=d['base_expiry'] or sub.device_limit!=d['base_devices'] or c.id!=d['cid'] or c.server_id!=d['profile']['panel'] or c.remote_user_uuid!=d['remote']:raise ValueError('Подписка изменилась. Нужен новый расчёт.')
            if not self.settings.is_owner(user.telegram_id) and not await enabled(self.repo):raise ValueError('Подключение сейчас выключено.')
            if d['tariff']:
                from app.services.bot_catalog import available
                target=next((t for t in await available(self.repo) if t.id==d['tariff']),None)
                if not target or (target.months,target.device_limit)!=(d['months'],d['devices']):raise ValueError('Тариф изменился. Откройте новый расчёт.')
            # Other bank renewals/upgrades must settle before this key changes.
            from app.services.recurring_cards import unsettled_card
            if await unsettled_card(self.repo,sub.id):raise ValueError('Сначала завершите текущий автоплатёж.')
            for p in await self.repo.session.scalars(select(Payment).where(Payment.user_id==user.id,Payment.status.in_(('order_created','order_pending','order_processing')))):
                if json.loads(await self.repo.setting(f'order:{p.id}','{}')).get('sid')==sub.id:raise ValueError('Сначала завершите оплату продления этого ключа.')
        if not d.get('held'):
            if mode=='wallet' and await self.repo.balance(user.id)<d['amount']:return await self.summary(user,token,d)
            d['mode']=mode;d['held']=True
            if mode=='wallet' and d['amount']:
                hold=await self.repo.add(BalanceTransaction(user_id=user.id,type='upgrade_hold',amount=-d['amount'],description=f'Резерв антиглушилки #{sub.id}'));d['hold']=hold.id
            await self.save(token,d);await self.repo.set_setting(f'bot_antig_open:{user.id}:{sub.id}',token)
        from app.services.vless_provision import control
        target=sorted(set(d['groups'])|set(d['profile']['squads']))
        request=dict(kind='bot_antig_renewal' if d['tariff'] else 'bot_antig_upgrade',active_squads=dict(base=d['groups'],target=target),amount=d['amount'])
        try:
            await control(self.repo,self.settings,c,'extend',datetime.fromisoformat(d['expiry']),renewal=dict(operation_key=ref,request=request,devices=d['devices'],traffic_limit=d['traffic']))
        except ValueError:
            op=await self.repo.session.get(VlessRenewal,operation_id(user.id,ref),populate_existing=True)
            if op and op.state!='failed':return await self.summary(user,token,d)
            if d.get('hold'):await self.repo.add(BalanceTransaction(user_id=user.id,type='upgrade_release',amount=d['amount'],description=f'Возврат резерва антиглушилки #{sub.id}'))
            d['held']=False;await self.save(token,d)
            if partner:return rich('Панель пока не подтвердила подключение. Долг не начислен.',
                                   [[('Повторить',f'ag:apply:{token}')],[('← Ключ',f'sub:{sub.id}')]])
            return rich('Панель не подтвердила изменение. Сумма доступна на балансе.',[[('🔄 Повторить',f'ag:{mode if mode in ("sale","free") else "apply"}:{token}')],[('← Ключ',f'sub:{sub.id}')]])
        if d.get('hold'):
            hold=await self.repo.session.get(BalanceTransaction,d['hold']);hold.type='subscription_upgrade';hold.description=f'Антиглушилка #{sub.id}'
        if partner and d['amount']:
            from app.services.partner_ledger import append
            await append(self.repo,user.id,ref,'antig',d['amount'],user.id,sid=sub.id,key_id=key.id)
        elif mode!='free' and d['amount']:
            payment=await self.repo.add(Payment(user_id=user.id,subscription_id=sub.id,amount=d['amount'],status='manual_paid' if mode=='sale' else 'balance_paid',provider_id=ref))
            if mode=='sale':await self.repo.add(BalanceTransaction(user_id=user.id,type='manual_sale_income',amount=d['amount'],description=f'Антиглушилка #{sub.id}'))
            from app.web.admin_reports import record_sale
            await record_sale(self.repo,payment,d['months'],d['devices'],renewal=True,source='telegram')
            snapshot=json.loads(await self.repo.setting(f'sale_snapshot:{payment.id}'));snapshot.update(antig_addon=True,tariff_name='Антиглушилка'+(' и продление' if d['tariff'] else ''),tariff_id=d['tariff'] or sub.tariff_id);await self.repo.set_setting(f'sale_snapshot:{payment.id}',json.dumps(snapshot))
        await self.repo.set_setting(f'bot_test_access:{sub.id}',json.dumps(d['profile']))
        await self.repo.set_setting(f'bot_antig_until:{sub.id}',d['expiry'])
        if d['tariff']:
            sub.tariff_id=d['tariff'];sub.kind='paid'
            from app.services.subscription_labels import refresh
            await refresh(self.repo,sub,key,duration(d['months']))
            from app.services.proxy_control import queue_included
            await queue_included(self.repo,sub)
        await self.repo.set_setting(f'auto_renew:{sub.id}','0')
        d['done']=True;d['held']=False;await self.save(token,d)
        return await self.summary(user,token,d)

    async def render(self,action,user):
        p=action.split(':');op=p[1]
        partner=self.settings.role(user.telegram_id)=='partner'
        if partner and op in ('ask','fund','sale','free'):
            raise PermissionError('Откройте антиглушилку из карточки ключа.')
        if op in ('menu','quote'):return await self.quote(user,int(p[2]),int(p[3]) if len(p)>3 else 0)
        if op=='ask':
            sub,_,_=await self.context(user,int(p[2]));tid=int(p[3])
            if sub.kind=='trial' and await has_access(self.repo,sub):
                return rich('🛡 <b>Продлить VPN с антиглушилкой?</b>\n\n'
                            'Антиглушилка входит в пробную подписку до её окончания. '
                            'Выберите, нужна ли она на платный срок. Ключ и ссылка сохранятся.',
                            [[('✅ С антиглушилкой',f'ag:quote:{sub.id}:{tid}')],
                             [('Только VPN',f'tariff:id{tid}:{sub.id}')],
                             [('← Выбор срока',f'renew:{sub.id}')]])
            return rich('🛡 <b>Добавить антиглушилку при продлении?</b>\n\nПодключим сразу до новой даты окончания. Доплата включает остаток текущего срока и выбранное продление.',[[('✅ Добавить',f'ag:quote:{sub.id}:{tid}')],[('Без дополнения',f'tariff:id{tid}:{sub.id}')],[('← Выбор срока',f'renew:{sub.id}')]])
        if op=='price':
            if not self.settings.is_owner(user.telegram_id):raise PermissionError('Недоступно.')
            sub=await self.repo.session.get(Subscription,int(p[2]))
            if not sub:raise ValueError('Подписка не найдена.')
            screen=rich('Введите итоговую цену антиглушилки за оставшийся срок в рублях.',[[('← Ключ',f'sub:{sub.id}')]])
            screen.admin_input_action=f'admin:input:antig_custom:{sub.id}';return screen
        token=p[2];d=await self.saved(user,token)
        if op in ('apply','sale','free'):return await self.apply(user,token,d,{'apply':'partner' if partner else 'wallet','sale':'sale','free':'free'}[op])
        if op=='fund':
            from app.services.topups import TopupService
            screen=await TopupService(self.repo,self.settings).prepare(user,max(100,d['amount']-await self.repo.balance(user.id)))
            for row in screen.rows:
                for _,target in row:
                    if target.startswith('topup_pay:'):await self.repo.set_setting('bot_antig_topup:'+target.split(':')[1],token)
            screen.rows.append([('← Расчёт',f'ag:show:{token}')]);return screen
        if op=='show':return await self.summary(user,token,d)
        raise ValueError('Действие недоступно.')

    async def set_price(self,user,sid,value):
        if not self.settings.is_owner(user.telegram_id):raise PermissionError('Недоступно.')
        sub=await self.repo.session.get(Subscription,sid)
        if not sub:raise ValueError('Подписка не найдена.')
        from app.services.bot_sale_price import rubles
        amount=rubles(value,allow_zero=True)
        await self.repo.set_setting(f'bot_antig_custom_price:{sid}',json.dumps(dict(amount=amount,expiry=utc(sub.expires_at).isoformat(),devices=sub.device_limit)))
        return rich('Цена дополнения сохранена: '+money(amount),[[('← Главное меню','home')]])


async def expire_addons(database,settings):
    # Do not extend a VPN or reactivate a blocked key while removing an expired
    # optional squad. Failed/uncertain PATCHes remain in the durable journal.
    async with database.sessions() as session:
        rows=list(await session.scalars(select(AppSetting).where(AppSetting.key.like('bot_antig_until:%'),AppSetting.value<=now().isoformat()).limit(5)))
        ids=[int(x.key.rsplit(':',1)[1]) for x in rows]
    for sid in ids:
        try:
            async with database.sessions() as session:
                async with session.begin():
                    from app.database.repositories import SQLRepository
                    from app.services.offers import lock_customer
                    repo=SQLRepository(session);sub=await session.get(Subscription,sid)
                    if not sub:continue
                    await lock_customer(repo,sub.user_id)
                    if await deadline(repo,sub)>now():continue
                    p=await profile(repo,sid)
                    if not p or len(p['squads'])!=2:
                        await repo.set_setting(f'bot_antig_until:{sid}','9999-12-31T00:00:00+00:00');continue
                    c=await session.scalar(select(VlessCredential).where(VlessCredential.subscription_id==sid));key=await session.get(AccessKey,c.access_key_id) if c else None
                    if not c or not key or c.status=='deleted':continue
                    desired,base=await access(repo,settings,c)
                    target=[v for v in base if v!=p['squads'][1]]
                    from app.services.vless_provision import control
                    request=dict(kind='bot_antig_expire',active_squads=dict(base=base,target=target))
                    active=utc(sub.expires_at)>now() and c.status=='active' and key.status=='active' and await repo.setting(f'subscription_status:{sid}','active')=='active'
                    await control(repo,settings,c,'extend',sub.expires_at,renewal=dict(operation_key='bot-antig:expire:'+str(sid)+':'+str(int((await deadline(repo,sub)).timestamp())),request=request,devices=c.device_limit,activate=active))
                    await repo.set_setting(f'bot_test_access:{sid}',json.dumps(dict(panel=p['panel'],squads=[p['squads'][0]])))
                    await repo.set_setting(f'bot_antig_until:{sid}','9999-12-31T00:00:00+00:00')
        except Exception:
            import logging
            logging.getLogger(__name__).warning('Expired Antiglushilka reconciliation postponed')
