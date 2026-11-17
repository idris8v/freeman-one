"""Owner-only renewal of an existing key with explicit sale/no-sale confirmation."""
import json,re,secrets
from datetime import timedelta,datetime
from decimal import Decimal, ROUND_CEILING
from sqlalchemy import select
from app.database.models import User,Subscription,AccessKey,VlessCredential,AppSetting,Payment,BalanceTransaction,AdminAudit,now
from app.services.vless_creation import utc
from app.services.vless_renewal import locked_context,unresolved
from app.services.admin_client_grant import rich
from app.services.bot_grant_config import parse_period,parse_combined
from app.services.bot_custom_sales import quote
from app.services.screens import money,duration
from app.services.bot_catalog import devices_label
from app.services.subscription_labels import expiry_date
from app.services.remnawave import RemnawaveApiError
from html import escape

class BotAdminRenew:
    def __init__(self,repo,settings):self.repo,self.settings,self.session=repo,settings,repo.session

    async def context(self,sid,actor):
        if not self.settings.is_owner(actor.telegram_id):raise PermissionError('Продление без продажи доступно только администратору.')
        sub=await self.session.get(Subscription,sid)
        c=await self.session.scalar(select(VlessCredential).where(VlessCredential.subscription_id==sid)) if sub else None
        if not c:raise ValueError('Рабочий VLESS-ключ не найден.')
        sub,key,c=await locked_context(self.repo,c)
        user=await self.session.get(User,sub.user_id)
        if not user or user.status!='active' or await self.repo.setting(f'subscription_removed:{sid}')=='1' or await self.repo.setting(f'subscription_transferred:{sid}')=='1':raise ValueError('Подписка недоступна.')
        return sub,key,c,user

    async def devices(self,sid,actor,offset=0):
        sub,key,c,user=await self.context(sid,actor)
        from app.services.bot_catalog import available,page_offset
        tariffs=await available(self.repo)
        groups=sorted({t.device_limit for t in tariffs}|({sub.device_limit} if sub.device_limit>0 else set()))
        offset=page_offset(offset,len(groups))
        rows=[[(f'📱 {devices_label(n)}',f'admin:renewcustom:periods:{sid}:{n}')] for n in groups[offset:offset+5]]
        if offset:rows.append([('← Предыдущие',f'admin:renewcustom:devices:{sid}:{offset-5}')])
        if offset+5<len(groups):rows.append([('Следующие →',f'admin:renewcustom:devices:{sid}:{offset+5}')])
        rows+=[[('⚙️ Свои устройства и срок',f'admin:input:renewcustom:{sid}:{sub.device_limit}')],[('← Ключ',f'admin:r:key:{c.id}')]]
        return rich('🔄 <b>Продление подписки</b>\n\nВыберите количество устройств или задайте свои устройства и срок одним сообщением.\n\nПосле выбора проверьте расчёт и укажите: учесть продажу или продлить бесплатно.',rows)

    async def periods(self,sid,devices,actor,offset=0):
        sub,key,c,user=await self.context(sid,actor)
        if not 1<=devices<=100:raise ValueError('Устройства: от 1 до 100.')
        from app.services.bot_catalog import available
        from app.services.admin_subscription import sale_terms
        tariffs=sorted((t for t in await available(self.repo) if t.device_limit==devices),key=lambda t:(t.months,t.id))
        from app.services.bot_catalog import page_offset
        offset=page_offset(offset,len(tariffs));rows=[]
        for t in tariffs[offset:offset+5]:
            terms=await sale_terms(self.repo,t,renewal=True)
            rows.append([(f'{duration(t.months)} · {money(terms["total"])}',f'admin:renewcustom:tariff:{sid}:{t.id}')])
        if not tariffs:rows=[[(duration(m),f'admin:renewcustom:period:{sid}:{devices}:{m}')] for m in (1,3,6,12)]
        if offset:rows.append([('← Предыдущие сроки',f'admin:renewcustom:periods:{sid}:{devices}:{offset-5}')])
        if offset+5<len(tariffs):rows.append([('Следующие сроки →',f'admin:renewcustom:periods:{sid}:{devices}:{offset+5}')])
        rows+=[[('⚙️ Свои устройства и срок',f'admin:input:renewcustom:{sid}:{devices}')],[('← Устройства',f'admin:renewcustom:devices:{sid}')]]
        return rich(f'🔄 <b>Продление · {devices_label(devices)}</b>\n\nШаг 2 из 2 — выберите срок.\n\nПродлевается этот же ключ. Оставшиеся дни не пропадут.',rows)

    async def prompt(self,sid,devices,actor):
        await self.context(sid,actor)
        if not 1<=devices<=100:raise ValueError('Устройства: от 1 до 100.')
        screen=rich(f'🔄 <b>Свои устройства и срок</b>\n\nТекущий лимит: {devices_label(devices)}\n\nВведите одним сообщением: <code>2, 20 дней</code> или <code>3, 12 часов</code>. Можно указать только срок, чтобы сохранить выбранный лимит: <code>20 дней</code>.\n\nУкажите цену третьим значением:\n<code>2, 20 дней, 1000</code>\n\nБез цены — бесплатно, без продажи и выручки. С ценой — подтверждение учёта продажи.',[[('← Выбор продления',f'admin:renewcustom:devices:{sid}')]])
        screen.admin_input_action=f'admin:input:renewcustom:{sid}:{devices}'
        return screen

    async def input(self,sid,devices,value,actor):
        if '|' not in value:
            from app.services.bot_grant_config import parse_sale
            if ',' in value:
                parsed,total=parse_sale(value)
                return await self.preview(sid,parsed['devices'],parsed['unit'],parsed['amount'],actor,total)
            unit,n=parse_period(value)
            return await self.preview(sid,devices,unit,n,actor,0)
        fields=value.split('|')
        if len(fields)>2:raise ValueError('Формат: 20 дней | 1000. Сумма необязательна.')
        if ',' in fields[0]:
            parsed=parse_combined(fields[0]);devices=parsed['devices'];unit,n=parsed['unit'],parsed['amount']
        else:unit,n=parse_period(fields[0])
        amount=None
        if len(fields)==2:
            raw=fields[1].strip().replace(',','.')
            if not re.fullmatch(r'\d{1,7}(?:\.\d{1,2})?',raw):raise ValueError('Введите сумму в рублях, до двух знаков после запятой.')
            amount=int(Decimal(raw)*100)
            if not 1<=amount<=100_000_000:raise ValueError('Сумма: от 0,01 до 1 000 000 ₽.')
        return await self.preview(sid,devices,unit,n,actor,amount)

    async def preview(self,sid,devices,unit,n,actor,amount=None,tariff=None):
        sub,key,c,user=await self.context(sid,actor)
        # Validate input limits even when invoked through a crafted callback.
        parse_period(f'{n} '+{'months':'месяцев','hours':'часов','days':'дней'}[unit])
        hours=n*720 if unit=='months' else n*24 if unit=='days' else n
        label=duration(n) if unit=='months' else f'{n} '+('ч.' if unit=='hours' else 'дн.')
        terms=quote(devices,hours=hours,label=label)
        total=amount if amount is not None else terms['total']
        expiry=max(now(),utc(sub.expires_at))+timedelta(hours=hours)
        token=secrets.token_hex(10)
        data=dict(actor=actor.id,user=user.id,sid=sid,cid=c.id,devices=devices,base_devices=c.device_limit,base_expiry=utc(sub.expires_at).isoformat(),expiry=expiry.isoformat(),hours=hours,label=label,total=total,until=now().timestamp()+600,done=False,tariff=tariff,base_total=total,antig=False)
        from app.services.bot_antig_addon import has_access
        data['antig_pending']=await has_access(self.repo,sub)
        await self.repo.set_setting('admin_custom_renew:'+token,json.dumps(data))
        return self.summary(token,data)

    def summary(self,token,data):
        total=data['total']
        sid,cid=data['sid'],data['cid']
        back=[('← Ключ',f'admin:r:key:{cid}')]
        base=f'admin:renewcustom:apply:{token}:'
        date=expiry_date(datetime.fromisoformat(data['expiry']))
        if data.get('addon_only'):
            enabled=data['antig']
            title='Подключить антиглушилку' if enabled else 'Отключить антиглушилку'
            text=f'🛡 <b>{title}</b>\n\nСрок ключа: <b>до {date}</b>.'
            if not enabled:
                return rich(text+'\n\nСсылка, устройства и основной VPN сохранятся. Отключение бесплатное.',[[('Отключить',base+'free')],back])
            text+=f'\nДоплата: <b>{money(total)}</b>\n\nАнтиглушилка будет действовать до той же даты, что и ключ.'
            rows=[[('Подключить и учесть '+money(total),base+'sale')]] if total else []
            return rich(text,rows+[[('Подключить бесплатно',base+'free')],[('← Варианты цены',f'admin:renewcustom:antig:{token}:p')],back])
        rows=[]
        if not data.get('antig_pending'):
            if total:
                rows.append([('✅ Продлить и учесть '+money(total),base+'sale')])
            rows.append([('🎁 Продлить бесплатно',base+'free')])
        label='Антиглушилка: '+('Да' if data.get('antig') else 'Выберите вариант' if data.get('antig_pending') else 'Нет')
        rows.extend([
            [('🛡 '+label,f'admin:renewcustom:antig:{token}:p')],
            [('✏️ Изменить устройства / срок / сумму',f'admin:input:renewcustom:{sid}:{data["devices"]}')],
            back,
        ])
        text=(f'🔄 <b>Продление подписки</b>\n\nУстройства: <b>{data["devices"]}</b>'
              f'\nДобавить: <b>{escape(data["label"])}</b>\nНовый срок: <b>до {date}</b>'
              f'\nЦена: <b>{money(total)}</b>')
        if data.get('antig'):
            text+=f'\nВ том числе антиглушилка: <b>{money(total-data["base_total"])}</b> до {date}.'
        elif data.get('antig_pending'):
            text+='\n\nВыберите, продлевать ли действующую антиглушилку.'
        return rich(text,rows)

    async def draft(self,token,actor,*,editing=False):
        if not self.settings.is_owner(actor.telegram_id):
            raise PermissionError('Недостаточно прав.')
        if not re.fullmatch(r'[0-9a-f]{20}',token):
            raise ValueError('Подтверждение недоступно.')
        row=await self.session.get(AppSetting,'admin_custom_renew:'+token,with_for_update=True,populate_existing=True)
        data=json.loads(row.value) if row else {}
        if data.get('actor')!=actor.id:
            raise PermissionError('Подтверждение другого администратора.')
        if editing and (data.get('mode') or data.get('done') or data['until']<now().timestamp()):
            raise ValueError('Откройте новый расчёт.')
        return row,data

    async def addon(self,sid,actor):
        from app.services.bot_antig_addon import has_access, access
        sub,key,c,user=await self.context(sid,actor)
        if utc(sub.expires_at)<=now():
            raise ValueError('Срок истёк. Настройте антиглушилку при продлении.')
        if key.status!='active' or c.status!='active':
            raise ValueError('Антиглушилка доступна для активного ключа.')
        token=secrets.token_hex(10)
        expiry=utc(sub.expires_at).isoformat()
        active=await has_access(self.repo,sub)
        data=dict(actor=actor.id,user=user.id,sid=sid,cid=c.id,devices=c.device_limit,
                  base_devices=c.device_limit,base_expiry=expiry,expiry=expiry,hours=0,
                  label='Антиглушилка',total=0,base_total=0,until=now().timestamp()+600,
                  done=False,tariff=None,addon_only=True,antig=not active)
        if active:
            profile,groups=await access(self.repo,self.settings,c)
            data.update(antig_profile=profile,antig_groups=groups,antig_snapshot=await self.addon_snapshot(sid))
        await self.repo.set_setting('admin_custom_renew:'+token,json.dumps(data))
        return self.summary(token,data) if active else self.antig_options(token,data)

    async def addon_snapshot(self,sid):
        return [await self.repo.setting(f'bot_test_access:{sid}'),await self.repo.setting(f'bot_antig_until:{sid}')]

    def antig_options(self,token,data,error=''):
        base=f'admin:renewcustom:antig:{token}:'
        rows=[
            [('Рассчитать автоматически',base+'a')],
            [('Ввести сумму вручную',f'admin:input:agfee:{token}')],
            [('Добавить без доплаты',base+'f')],
        ]
        if not data.get('addon_only'):
            rows.append([('Без антиглушилки на новый срок',base+'0')])
        rows.append([('← Ключ',f'admin:r:key:{data["cid"]}') if data.get('addon_only')
                     else ('← К продлению',base+'v')])
        text='🛡 <b>Антиглушилка</b>\n\nВыберите доплату. При подключении срок совпадёт со сроком подписки.'
        if error:
            text+='\n\n'+escape(error)
        return rich(text,rows)

    async def fee_prompt(self,token,actor,error=''):
        _,data=await self.draft(token,actor,editing=True)
        text='Введите доплату за антиглушилку в рублях. Например, 100 или 150,50.'
        if error:
            text=escape(error)+'\n\n'+text
        screen=rich(text,[[('← Варианты цены',f'admin:renewcustom:antig:{token}:p')]])
        screen.admin_input_action=f'admin:input:agfee:{token}'
        return screen

    async def fee_input(self,token,value,actor):
        from app.services.bot_sale_price import rubles
        try:
            amount=rubles(value,allow_zero=True)
            return await self.configure_antig(token,actor,'m',amount)
        except ValueError as error:
            return await self.fee_prompt(token,actor,str(error))

    async def configure_antig(self,token,actor,choice,amount=None):
        row,data=await self.draft(token,actor,editing=True)
        if choice=='v':
            return self.summary(token,data)
        if data.get('addon_only') and not data['antig']:
            raise ValueError('Выбран сценарий отключения антиглушилки.')
        if choice=='p':
            return self.antig_options(token,data)
        if choice not in ('0','1','a','f','m') or (choice=='0' and data.get('addon_only')) or (choice=='m' and amount is None):
            raise ValueError('Некорректный выбор.')
        sub,key,c,_=await self.context(data['sid'],actor)
        if utc(sub.expires_at).isoformat()!=data['base_expiry'] or c.device_limit!=data['base_devices']:
            raise ValueError('Подписка изменилась. Откройте новый расчёт.')
        if choice!='0':
            from app.services.bot_antig_addon import access, rate_remaining
            from app.services.bot_antig_price import monthly, tariff_total
            try:
                if choice in ('1','a'):
                    amount=await rate_remaining(self.repo,sub,now())
                    if data['tariff']:
                        amount+=await tariff_total(self.repo,await self.repo.tariff_by_id(data['tariff']))
                    elif data['hours']:
                        rate=await monthly(self.repo)
                        amount+=int((Decimal(rate)*data['hours']/720).quantize(Decimal('1'),rounding=ROUND_CEILING))
                elif choice=='f':
                    amount=0
                if not 0<=amount<=100_000_000:
                    raise ValueError('Доплата слишком велика. Укажите сумму вручную.')
                profile,groups=await access(self.repo,self.settings,c)
            except ValueError as error:
                return self.antig_options(token,data,str(error))
            except RemnawaveApiError:
                return self.antig_options(token,data,'Панель не подтвердила настройки антиглушилки. Попробуйте ещё раз.')
            data.update(antig_profile=profile,antig_groups=groups,antig_snapshot=await self.addon_snapshot(sub.id))
        data['antig']=choice!='0'
        data['antig_pending']=False
        data['total']=data.get('base_total',data['total'])+(amount if choice!='0' else 0)
        if data['total']>100_000_000:
            return self.antig_options(token,data,'Итоговая сумма слишком велика.')
        await self.repo.set_setting(row.key,json.dumps(data))
        return self.summary(token,data)

    async def render(self,action,actor):
        p=action.split(':');op=p[2]
        if op=='devices':return await self.devices(int(p[3]),actor,int(p[4]) if len(p)>4 else 0)
        if op=='tariff':
            from app.services.bot_catalog import available
            from app.services.admin_subscription import sale_terms
            tariff=next((t for t in await available(self.repo) if t.id==int(p[4])),None)
            if not tariff:raise ValueError('Тариф больше недоступен.')
            terms=await sale_terms(self.repo,tariff,renewal=True)
            return await self.preview(int(p[3]),tariff.device_limit,'months',tariff.months,actor,terms['total'],tariff.id)
        if op=='periods':return await self.periods(int(p[3]),int(p[4]),actor,int(p[5]) if len(p)>5 else 0)
        if op=='period':return await self.preview(int(p[3]),int(p[4]),'months',int(p[5]),actor)
        if op=='addon':return await self.addon(int(p[3]),actor)
        if op=='antig':return await self.configure_antig(p[3],actor,p[4])
        if op=='apply':
            _,data=await self.draft(p[3],actor)
            try:
                return await self.apply(p[3],p[4],actor)
            except ValueError as error:
                return rich('⚠️ <b>Операция не завершена</b>\n\n'+escape(str(error)),
                            [[('🔄 Повторить',action)],[('← Ключ',f'admin:r:key:{data["cid"]}')]])
        raise ValueError('Действие недоступно.')

    async def apply(self,token,mode,actor):
        if mode not in ('sale','free'):
            raise ValueError('Подтверждение недоступно.')
        row,d=await self.draft(token,actor)
        user=await self.session.get(User,d['user'],with_for_update=True,populate_existing=True)
        sub,key,c,user=await self.context(d['sid'],actor)
        back=f'admin:r:key:{c.id}'
        if d.get('done'):return rich('✅ Операция уже выполнена.',[[('🔑 Ключ',back)]])
        addon_only=d.get('addon_only',False)
        changes_addon=addon_only or d.get('antig')
        if d.get('antig_pending') or (addon_only and not d.get('antig_profile')):
            raise ValueError('Сначала выберите вариант антиглушилки.')
        if addon_only and (utc(sub.expires_at)<=now() or key.status!='active' or c.status!='active'):
            raise ValueError('Антиглушилка доступна для активного ключа.')
        if changes_addon and d.get('antig_snapshot') is not None and d['antig_snapshot']!=await self.addon_snapshot(sub.id):
            raise ValueError('Антиглушилка уже изменилась. Откройте новый расчёт.')
        if mode=='sale' and d.get('total',0)<=0:raise ValueError('Цена не указана. Доступно бесплатное продление.')
        if d.get('mode') and d['mode']!=mode:raise ValueError('Этот запрос уже запущен в другом режиме. Завершите исходное подтверждение.')
        operation_key=('bot-antig:admin-renew:' if changes_addon else 'bot-custom-renew:')+token
        pending=await unresolved(self.repo,user,[operation_key])
        if d['until']<now().timestamp() and not pending:raise ValueError('Подтверждение устарело. Выберите срок заново.')
        if utc(sub.expires_at).isoformat()!=d['base_expiry'] or c.device_limit!=d['base_devices'] or c.id!=d['cid']:raise ValueError('Ключ уже изменился. Откройте новый расчёт.')
        d['mode']=mode;await self.repo.set_setting(row.key,json.dumps(d))
        from app.services.vless_provision import control
        # Existing durable renewal preserves traffic and encodes the spare slot.
        request=dict(kind='admin_custom_renew',hours=d['hours'],mode=mode,amount=d['total'] if mode=='sale' else 0)
        if changes_addon:
            groups=set(d['antig_groups'])
            default,antig=d['antig_profile']['squads']
            if d['antig']:
                groups.update((default,antig))
            else:
                groups.discard(antig)
            request['kind']=('bot_antig_upgrade' if d['antig'] else 'bot_antig_expire') if addon_only else 'bot_antig_renewal'
            request['active_squads']=dict(base=d['antig_groups'],target=sorted(groups))
        else:
            from app.services.bot_antig_test import profile
            old_access=await profile(self.repo,sub.id)
            if old_access and len(old_access['squads'])==2 and not await self.repo.setting(f'bot_antig_until:{sub.id}'):
                await self.repo.set_setting(f'bot_antig_until:{sub.id}',utc(sub.expires_at).isoformat())
        await control(self.repo,self.settings,c,'extend',datetime.fromisoformat(d['expiry']),renewal=dict(operation_key=operation_key,request=request,devices=d['devices'],activate=key.status=='active'))
        if changes_addon:
            profile=d['antig_profile']
            if not d['antig']:
                profile={**profile,'squads':profile['squads'][:1]}
            await self.repo.set_setting(f'bot_test_access:{sub.id}',json.dumps(profile))
            await self.repo.set_setting(f'bot_antig_until:{sub.id}',d['expiry'] if d['antig'] else '9999-12-31T00:00:00+00:00')
        if not addon_only:
            sub.device_limit=c.device_limit=d['devices']
            sub.expires_at=key.expires_at=c.expires_at=datetime.fromisoformat(d['expiry'])
            sub.tariff_id=d.get('tariff')
            from app.services.subscription_labels import refresh
            await refresh(self.repo,sub,key,d['label'])
        if d['devices']!=d['base_devices']:await self.repo.set_setting(f'auto_renew:{sub.id}','0')
        notice=dict(user=user.id,recorded_by=actor.id,subscription_id=sub.id,devices=d['devices'],added_term=d['label'],previous_expiry=d['base_expiry'],expires_at=d['expiry'],name=await self.repo.setting(f'subscription_name:{sub.id}','') or key.name,subscription_url=c.subscription_url,total=d['total'] if mode=='sale' else 0,accounting=mode)
        if mode=='sale':
            ref='bot-custom-renew:'+token
            assert not await self.session.scalar(select(Payment.id).where(Payment.provider_id==ref))
            payment=await self.repo.add(Payment(user_id=user.id,subscription_id=sub.id,amount=d['total'],status='manual_paid',provider_id=ref))
            description='Антиглушилка для ключа' if addon_only else 'Ручное продление подписки'
            await self.repo.add(BalanceTransaction(user_id=actor.id,type='manual_sale_income',amount=d['total'],description=f'{description} #{sub.id}'))
            snap=dict(kind='vpn',source='manual',custom=True,renewal=True,issuance_kind='manual_sale',sale_category='custom_renewal',months=d['hours']//720 if d['hours']%720==0 else 0,devices=d['devices'],total=d['total'],tariff_name='Продление · '+d['label'],recorded_by=actor.id,key_id=key.id,subscription_id=sub.id,paid_at=now().isoformat())
            if addon_only:snap.update(renewal=False,sale_category='antig_addon',tariff_name='Антиглушилка')
            snap.update(notice)
            await self.repo.set_setting(f'sale_snapshot:{payment.id}',json.dumps(snap));d['payment']=payment.id
        elif not addon_only:
            from app.services.activity import record
            await record(self.repo,'subscription.renewed:'+token,json.dumps(notice))
        await self.repo.add(AdminAudit(actor_telegram_id=actor.telegram_id,action='bot_antig_admin' if addon_only else 'bot_custom_renew',target=str(sub.id),details=f'{mode}; {d["hours"]} hours; {d["devices"]} devices; '+(str(d['total'])+' kopecks' if mode=='sale' else 'no sale or wallet income')))
        from app.services.admin_client_notices import queue
        title=('🛡 Антиглушилка подключена' if d['antig'] else '🛡 Антиглушилка отключена') if addon_only else '🔄 Подписка продлена'
        body='Действует до '+expiry_date(sub.expires_at)+'.' if not addon_only or d['antig'] else ''
        await queue(self.repo,user,actor,'renew:'+token,title,body,button=('Открыть подписку',f'sub:{sub.id}'))
        if not addon_only:
            from app.services.proxy_control import queue_included
            await queue_included(self.repo,sub)
        d['done']=True
        await self.repo.set_setting(row.key,json.dumps(d))
        if addon_only:
            result='Антиглушилка подключена до '+expiry_date(sub.expires_at) if d['antig'] else 'Антиглушилка отключена'
            return rich('✅ <b>'+result+'</b>',[[('🔑 Открыть ключ',back)]])
        return rich('✅ <b>Подписка продлена</b>\n\n'+devices_label(d['devices'])+' · добавлено '+escape(d['label'])+'\n\n'+('Продажа учтена: '+money(d['total']) if mode=='sale' else 'Без учёта продажи. Выручка и баланс не изменились.'),[[('🔑 Открыть ключ',back)]])

