"""Administrator-created bearer gifts; durable first-recipient binding, no sale."""
import hashlib,json,re,secrets
from datetime import timedelta,datetime
from html import escape
from urllib.parse import urlencode
from sqlalchemy import select
from app.database.models import AppSetting,User,Subscription,VlessCredential,ProxyCredential,AdminAudit,AccessKey,now
from app.services.admin_client_grant import rich
from app.services.bot_grant_config import parse_combined,period_label
from app.services.vless_creation import durable_session,utc
from app.services.screens import Screen

PREFIX='bot_gift_link:'

def gift_key(token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{32}',token):raise ValueError('Ссылка подарка недействительна.')
    return PREFIX+hashlib.sha256(token.encode()).hexdigest()

def styled(text,rows,styles=None):
    screen=rich(text,rows);screen.button_options=styles or {};return screen

class BotGiftLinks:
    def __init__(self,repo,settings):self.repo,self.settings,self.session=repo,settings,repo.session
    def require(self,actor):
        if not self.settings.is_owner(actor.telegram_id):raise PermissionError('Подарки по ссылке создаёт только администратор.')
    def link(self,token):return f'https://t.me/{self.settings.bot_username}?start=fg_{token}'
    async def data(self,token,actor):
        self.require(actor);raw=await self.repo.setting(gift_key(token));d=json.loads(raw) if raw else {}
        if d.get('paid'):raise ValueError('Оплаченный подарок доступен в разделе «Мои подарки».')
        if d.get('actor')!=actor.id:raise PermissionError('Подарок другого администратора.')
        return d
    async def prompt(self,kind,actor):
        self.require(actor)
        if kind not in ('vpn','proxy'):raise ValueError('Выберите VPN или Proxy.')
        title='VPN' if kind=='vpn' else 'Telegram Proxy'
        return styled(f'🎁 <b>Подарить {title}</b>\n\nУкажите подключения и срок одним сообщением:\n<code>2, 30 дней</code>\n\nПолучателя выбирать не нужно. Срок начнётся после получения подарка. Выдача без учёта продажи.'+('\n\nДля VPN доступны также часы и месяцы.' if kind=='vpn' else '\n\nДля Proxy укажите срок в днях; подключения ограничиваются уникальными IP.'),[[('← Подарки','admin:giftlink:home:'+kind)]])
    async def input(self,kind,value,actor):
        self.require(actor)
        if kind not in ('vpn','proxy'):raise ValueError('Тип подарка недоступен.')
        p=parse_combined(value,proxy=kind=='proxy');token=secrets.token_urlsafe(24)
        hours=p['amount']*(720 if p['unit']=='months' else 24 if p['unit']=='days' else 1)
        d=dict(actor=actor.id,kind=kind,devices=p['devices'],hours=hours,label=period_label(p),status='draft',expires=(now()+timedelta(days=3)).isoformat())
        if kind=='proxy':
            from app.services.proxies import ProxyService
            servers=await ProxyService(self.repo,self.settings).matching_servers('paid')
            if not servers:raise ValueError('Нет доступного сервера Telegram Proxy. Ссылка не создана.')
            d.update(server=servers[0].id,tcp=max(p['devices'],servers[0].tcp_connection_limit))
        await self.repo.set_setting(gift_key(token),json.dumps(d))
        action='admin:giftlink:make:'+token
        return styled('🎁 <b>Проверьте подарок</b>\n\n'+('VPN' if kind=='vpn' else 'Telegram Proxy')+f' · {p["devices"]} подключений · {escape(d["label"])}\n\nПервый получатель ссылки заберёт подарок. До подтверждения ничего не выдаётся.\n\nБез оплаты, выручки и изменений баланса.',[[('🎁 Создать подарочную ссылку',action)],[('✏️ Изменить',f'admin:input:giftlink:{kind}')]],{action:{'style':'success'}})
    async def render(self,action,actor):
        self.require(actor);p=action.split(':');op=p[2]
        if op=='home':
            if len(p)>3 and p[3]=='proxy':
                from app.services.bot_proxy_grant import BotProxyGrant
                return await BotProxyGrant(self.repo,self.settings).render('admin:proxygrant:choose',actor)
            from app.services.customer_gifts import CustomerGifts
            return await CustomerGifts(self.repo,self.settings).render('cg:hub',actor)
        if op in ('list','item','access','remove','removeok'):
            rows=[]
            entries=[]
            for row in await self.session.scalars(select(AppSetting).where(AppSetting.key.startswith(PREFIX))):
                value=json.loads(row.value)
                if value.get('actor')==actor.id and not value.get('paid') and not value.get('hidden') and value.get('kind') in ('vpn','proxy') and (op!='list' or value['kind']==p[3]):entries.append((row.key,value))
            entries.sort(key=lambda item:(item[1].get('expires',''),item[0]),reverse=True)
            def status(d):
                if d['status']=='claimed':return '✅ Получен'
                if d['status']=='claiming':return '⏳ Выдаётся'
                if d['status']=='cancelled':return '🚫 Отменён'
                if datetime.fromisoformat(d['expires'])<=now():return '⌛ Ссылка истекла'
                return '🎁 Ожидает получения' if d['status']=='open' else '✏️ Черновик'
            if op in ('item','access','remove','removeok'):
                matched=[(key,d) for key,d in entries if key[len(PREFIX):].startswith(p[3])]
                if len(matched)!=1:raise ValueError('Подарок не найден.')
                key,d=matched[0]
                offset=max(0,int(p[4])) if len(p)>4 else 0
                item='admin:giftlink:item:'+p[3]+':'+str(offset)
                back='admin:giftlink:list:'+d['kind']+':'+str(offset)
                if op in ('remove','removeok'):
                    if d.get('user') or d.get('status') in ('claiming','claimed'):raise ValueError('Подарок уже активируется или получен. Удалить приглашение нельзя.')
                    if op=='remove':
                        target='admin:giftlink:removeok:'+p[3]+':'+str(offset)
                        return styled('🗑 <b>Удалить подарок?</b>\n\nПриглашение исчезнет из списка, ссылка перестанет работать. Полученный доступ не удаляется.',[[('🗑 Удалить',target)],[('← Отмена',item)]],{target:{'style':'danger'}})
                    async with durable_session(self.repo) as s,s.begin():
                        row=await s.get(AppSetting,key,with_for_update=True);current=json.loads(row.value)
                        if current.get('actor')!=actor.id or current.get('paid') or current.get('user') or current.get('status') in ('claiming','claimed'):raise ValueError('Подарок уже активируется или недоступен.')
                        current.update(status='cancelled',hidden=True);row.value=json.dumps(current)
                    return styled('✅ Подарок удалён из списка. Ссылка недействительна.',[[('← Подарки',back)]])
                if op=='access':
                    from app.services.proxies import ProxyService
                    if d['kind']!='proxy':raise ValueError('Это не подарок Telegram Proxy.')
                    if not d.get('credential'):raise ValueError('Подарок ещё не получен.')
                    screen=await ProxyService(self.repo,self.settings).render(f'admin:p:cred:{d["credential"]}',actor)
                    screen.rows=[row for row in screen.rows if not any(label.startswith('←') for label,_ in row)]+[[('← Подарок',item)]]
                    return screen
                text='🎁 <b>'+('Подарочный Telegram Proxy' if d['kind']=='proxy' else 'Подарочный VPN')+'</b>\n\n'+status(d)+f'\n\nПодключений: {d["devices"]}\nСрок: {escape(d["label"])}'
                if d.get('user'):
                    user=await self.session.get(User,d['user'])
                    text+='\n\nПолучатель: '+escape(user.display_name if user else 'Недоступен')
                if d.get('credential') and d['kind']=='proxy':rows.append([('🌐 Открыть прокси','admin:giftlink:access:'+p[3]+':'+str(offset))])
                if not d.get('user'):rows.append([('🗑 Удалить подарок','admin:giftlink:remove:'+p[3]+':'+str(offset))])
                rows.append([('← Подарки',back)])
                return styled(text,rows)
            offset=max(0,int(p[4])) if len(p)>4 else 0
            offset=min(offset,max(0,(len(entries)-1)//5*5))
            for key,d in entries[offset:offset+5]:rows.append([(status(d)+' · '+d['label'], 'admin:giftlink:item:'+key[len(PREFIX):][:16]+':'+str(offset))])
            if offset:rows.append([('← Предыдущие',f'admin:giftlink:list:{p[3]}:{max(0,offset-5)}')])
            if offset+5<len(entries):rows.append([('Следующие →',f'admin:giftlink:list:{p[3]}:{offset+5}')])
            rows.append([('← Telegram Proxy','p:home')] if p[3]=='proxy' else [('← Подарить VPN','cg:hub')])
            return styled('📨 <b>Подарочные '+('VPN' if p[3]=='vpn' else 'прокси')+'</b>\n\n'+('Выберите подарок. Показаны ваши ссылки, включая уже полученные.' if entries else 'Подарочных ссылок пока нет.'),rows)
        token=p[3];d=await self.data(token,actor)
        if op=='preview':
            return styled('🎁 <b>Параметры подарка</b>\n\n'+('VPN' if d['kind']=='vpn' else 'Telegram Proxy')+' · '+str(d['devices'])+' подключений · '+escape(d['label']),[[('🎁 Создать / открыть ссылку','admin:giftlink:make:'+token)],[('✏️ Изменить','admin:input:giftlink:'+d['kind'])],[('← Назад','admin:giftlink:home:'+d['kind'])]])
        if op=='make':
            if d['status']=='draft':d['status']='open';await self.repo.set_setting(gift_key(token),json.dumps(d))
        elif op=='cancel':
            confirm='admin:giftlink:cancelok:'+token
            return styled('Удалить неиспользованный подарок? Он исчезнет из списка, ссылка перестанет работать.',[[('Удалить подарок',confirm)],[('← Назад','admin:giftlink:view:'+token)]],{confirm:{'style':'danger'}})
        elif op=='cancelok':
            async with durable_session(self.repo) as s,s.begin():
                row=await s.get(AppSetting,gift_key(token),with_for_update=True);current=json.loads(row.value)
                if current.get('user'):raise ValueError('Подарок уже закреплён за получателем. Отмена приглашения недоступна.')
                current.update(status='cancelled',hidden=True);row.value=json.dumps(current);d=current
            return styled('✅ Подарочная ссылка отменена.',[[('🎁 Подарки','admin:giftlink:list:'+d['kind']+':0')]])
        if d['status'] not in ('open','claiming','claimed'):raise ValueError('Подарочная ссылка ещё не готова или отменена.')
        title='VPN-подписка' if d['kind']=='vpn' else 'Telegram Proxy'
        message=f'🎁 Вам в подарок {title}!\n\nСрок: {d["label"]}\nПодключений: {d["devices"]}\n\nОткройте ссылку и нажмите «Старт», чтобы получить доступ. Срок начнётся при получении.'
        link=self.link(token);claim='url:'+link
        full_message=f'🎁 Для вас подарок — {title}!\n\nСрок: {d["label"]} · Подключений: {d["devices"]}\n\nПерейдите по ссылке и нажмите «Старт» для получения:\n'+link+'\n\nСрок начнётся при получении.'
        if len(full_message)>256:full_message=f'🎁 Подарок: {title}\n{d["label"]} · {d["devices"]} подключений\nДля получения откройте бота:\n'+link
        copy='copy:'+full_message
        if op=='post':
            return styled('🎁 <b>Для вас подарок!</b>\n\n'+escape(message.split('\n')[0])+'</b>\n\n'+escape(message.split('\n\n',1)[1])+'\n\nПерейдите по ссылке для получения:\n<code>'+escape(link)+'</code>\n\n<blockquote>Одноразовый подарок. Получить может первый открывший ссылку.</blockquote>',[[('🎁 Получить подарок',claim)]],{claim:{'style':'success'}})
        share='url:https://t.me/share/url?'+urlencode(dict(url=link,text=message))
        post='admin:giftlink:post:'+token
        return styled('🎁 <b>Приглашение для друга готово</b>\n\n<blockquote>'+escape(title)+f' · {d["devices"]} подключений · {escape(d["label"])}'+ '</blockquote>\n\n<code>'+escape(link)+'</code>\n\nСсылка действует 3 дня, одноразовая. Получатель заранее не нужен. Срок подписки начинается при получении.\n\n«Готовое сообщение» можно переслать вместе с кнопкой получения. «Отправить другу» откроет выбор чата в Telegram.',[[('↗️ Отправить другу',share)],[('📋 Скопировать текст с ссылкой',copy)],[('🎁 Карточка для пересылки',post)],[('Отменить ссылку','admin:giftlink:cancel:'+token)],[('← Параметры','admin:giftlink:preview:'+token)]],{share:{'style':'primary'},post:{'style':'success'},'admin:giftlink:cancel:'+token:{'style':'danger'}})

    async def reserve(self,token,user):
        if user.status!='active':raise ValueError('Аккаунт недоступен.')
        # This transaction commits the recipient and subscription identity BEFORE
        # any provider request. A failed outer request cannot give the gift away twice.
        async with durable_session(self.repo) as s,s.begin():
            row=await s.get(AppSetting,gift_key(token),with_for_update=True)
            if not row:raise ValueError('Подарок не найден.')
            d=json.loads(row.value)
            if d.get('paid'):
                from app.database.models import Payment
                payment=await s.get(Payment,d['payment'])
                if not payment or payment.user_id!=d['buyer'] or payment.status not in ('balance_paid','order_paid'):raise ValueError('Оплата этого подарка больше не действует.')
            if d.get('user'):
                if d['user']!=user.id:raise ValueError('Этот подарок уже получил другой человек.')
                if d['status'] not in ('claiming','claimed'):raise ValueError('Подарок недоступен.')
                return d
            if d['status']!='open' or not d.get('paid') and datetime.fromisoformat(d['expires'])<=now():raise ValueError('Подарочная ссылка отменена или срок её действия истёк.')
            account=await s.get(User,user.id)
            if not account or account.status!='active':raise ValueError('Аккаунт недоступен.')
            if d.get('paid'):
                from app.services.billing import extend
                end=extend(now(),d['months'])
            else:end=now()+timedelta(hours=d['hours'])
            d.update(user=user.id,status='claiming',end=end.isoformat())
            if d['kind']=='vpn':
                sub=Subscription(user_id=user.id,kind='paid' if d.get('paid') else 'admin',tariff_id=d.get('tariff'),device_limit=d['devices'],expires_at=datetime.fromisoformat(d['end']))
                s.add(sub);await s.flush();d['sid']=sub.id
            row.value=json.dumps(d)
            return d

    async def claim(self,token,user):
        d=await self.reserve(token,user)
        await self.session.get(User,user.id,with_for_update=True,populate_existing=True)
        row=await self.session.get(AppSetting,gift_key(token),populate_existing=True);d=json.loads(row.value)
        expiry=datetime.fromisoformat(d['end'])
        if expiry<=now():raise ValueError('Срок подаренной подписки закончился.')
        if d['kind']=='vpn':
            from app.services.vless_provision import ensure,connection
            sub=await self.session.get(Subscription,d['sid'],populate_existing=True)
            if not sub or sub.user_id!=user.id:raise ValueError('Подарок требует проверки поддержки.')
            if d.get('paid') and d['status']!='claimed':await self.repo.set_setting(f'subscription_traffic_limit:{sub.id}',str(d['traffic']))
            c=await ensure(self.repo,self.settings,sub,user,operation_key='gift-link:'+gift_key(token))
            if not c:raise ValueError('Выдача ещё не подтверждена. Откройте ту же ссылку позже; второй подарок не создаётся.')
            key=await self.session.get(AccessKey,c.access_key_id)
            if d['status']!='claimed':
                key.name='Подарок · '+d['label']
                await self.repo.set_setting(f'subscription_name:{sub.id}',key.name)
                await self.repo.set_setting(f'subscription_duration:{sub.id}',d['label'])
            from app.services.proxy_control import queue_included
            await queue_included(self.repo,sub)
            screen=await connection(self.repo,self.settings,sub,user)
            screen.text='🎁 <b>Ваш подарок получен!</b>\n\n'+screen.text
        else:
            from app.services.proxies import ProxyService
            from app.services.proxy_control import meta
            service=ProxyService(self.repo,self.settings);server=await service.server(d['server'])
            c=await self.session.get(ProxyCredential,d['credential']) if d.get('credential') else None
            if c:
                if c.user_id!=user.id or c.status!='active' or utc(c.expires_at)<=now():raise ValueError('Подаренный доступ больше не активен.')
            else:c,link=await service.provision(server,user,expiry,d['devices'],d['tcp'],'paid',connection_token='gift_'+hashlib.sha256(token.encode()).hexdigest()[:20])
            if d['status']!='claimed':
                m=await meta(self.repo,c);m.source='admin';m.ip_limit=d['devices']
                old=await self.repo.setting(f'proxy_paid_until:{user.id}')
                if not old or old<expiry.isoformat():await self.repo.set_setting(f'proxy_paid_until:{user.id}',expiry.isoformat())
            connect='url:'+service.public_link(service.decrypt(c.link))
            screen=styled('🎁 <b>Ваш Telegram Proxy готов!</b>\n\n<blockquote>'+escape(d['label'])+f' · {d["devices"]} подключений (уникальных IP)</blockquote>\n\nНажмите кнопку ниже и подтвердите подключение в Telegram. VPN для работы Telegram не нужен.\n\nНе передавайте ссылку подключения другим людям: лимит общий для вашего доступа.',[[('✈️ Подключить Telegram Proxy',connect)],[('🌐 Мои прокси','p:home')]],{connect:{'style':'primary'}})
        if d['status']!='claimed':
            d.update(status='claimed',credential=c.id);await self.repo.set_setting(gift_key(token),json.dumps(d))
            await self.repo.add(AdminAudit(actor_telegram_id=user.telegram_id,action='gift_link_claim',target=str(user.id),details=f'{d["kind"]}; gift received; no sale'))
        screen.structured=True
        return screen
