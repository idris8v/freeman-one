from app.services.subscription_labels import expiry_date
"""Owner-only search. Input URLs are identifiers, never network destinations."""
import asyncio,json,re
import unicodedata
from datetime import timezone
from html import escape
from urllib.parse import urlsplit,urlunsplit,unquote
from uuid import UUID
from zoneinfo import ZoneInfo
from sqlalchemy import select,or_,func,cast,String
from aiogram.types import InputRichMessage
from app.database.models import (User,AccessKey,Subscription,VlessCredential,VpnCredential,Payment,AppSetting,CustomerContact,Device,RemnawaveServer,now)
from app.services.screens import Screen,time_remaining
from app.services.accounts import display_freeman_id,find_freeman_id
from app.services.bot_catalog import rich_table,devices_label

PAID=('succeeded','order_paid','balance_paid','stars_paid')
PROMPT='🔎 <b>Глобальный поиск</b>\n\nПришлите ссылку подписки или VLESS-ключ. Можно искать по полному коду после последнего слэша ссылки, Freeman ID, Telegram ID, email, логину, имени клиента или названию ключа.\n\nДля точного поиска: «ключ 123» или «подписка 123».\n\nСсылку или ключ можно отправить прямо в личный чат с ботом, когда не открыт другой ввод.'

def telegram_identifier(value):
    # Explicit prefixes avoid collisions with internal customer/key IDs.
    value=unicodedata.normalize('NFKC',value).strip().replace('−','-').replace('–','-').replace('—','-')
    match=re.fullmatch(r'(?:(?:telegram|tg|телеграм)(?:[\s_-]*(?:id|айди))?|id|айди)[\s:#=\-]*([0-9]{1,19})',value,re.I)
    if not match:
        match=re.fullmatch(r'tg://user\?id=([0-9]{1,19})',value,re.I)
    if match and 0<int(match[1])<2**63:return int(match[1])
    return None

def identifier(value):
    value=value.strip()
    telegram_id=telegram_identifier(value)
    if telegram_id is not None:return 'telegram',telegram_id
    # Import links wrap the subscription URL; remove the wrapper without fetching it.
    for _ in range(3):
        decoded=unquote(value)
        if re.match(r'^(?:happ|hiddify|v2raytun)://',decoded,re.I):
            pos=re.search(r'https?://',decoded,re.I)
            if pos:value=decoded[pos.start():];continue
        break
    match=re.search(r'(?:vless|https?)://[^\s<>]+',value,re.I)
    if match:value=match.group(0).strip('"\'<>').rstrip('.,;')
    if value.lower().startswith('vless://'):
        try:return 'vless',str(UUID(urlsplit(value).username or ''))
        except (ValueError,AttributeError):raise ValueError('Не удалось прочитать VLESS-ключ. Пришлите его целиком или ссылку подписки.')
    if re.match(r'^https?://',value,re.I):return 'url',value
    for kind,pattern in [('key',r'(?:ключ|key|k)\s*[:# -]?\s*(\d+)'),('sub',r'(?:подписка|subscription|sub)\s*[:# -]?\s*(\d+)')]:
        m=re.fullmatch(pattern,value,re.I)
        if m:return kind,int(m[1])
    return 'text',value

def automatic(value):
    text=(value or '').strip()
    return bool(telegram_identifier(text) is not None or re.fullmatch(r'@[A-Za-z0-9_]{1,32}',text) or (re.fullmatch(r'[0-9]{1,19}',text) and 0<int(text)<2**63) or re.search(r'(?:vless|https?)://',text,re.I) or re.match(r'^(?:happ|hiddify|v2raytun)://',text,re.I) or re.fullmatch(r'(?i)(?:FO-|FRM-|Freeman\s+)\d{1,12}',text) or re.fullmatch(r'(?i)(?:ключ|key|подписка|sub)\s*[:# -]?\s*\d+',text))

def canonical(value):
    try:
        u=urlsplit(value)
        if u.scheme.lower() not in ('http','https') or not u.hostname or u.username or u.password:return None
        return (u.scheme.lower(),u.hostname.lower(),u.port or (443 if u.scheme.lower()=='https' else 80),unquote(u.path).rstrip('/'),u.query)
    except ValueError:return None

class BotAdminSearch:
    def __init__(self,repo,settings):self.repo,self.settings,self.session=repo,settings,repo.session
    def require(self,actor):
        if not self.settings.is_owner(actor.telegram_id):raise PermissionError('Поиск доступен только администратору.')
    async def telegram_user(self,identity,visible):
        user=await self.session.scalar(select(User).where(User.telegram_id==identity,*visible))
        if user:return user
        matches=list(await self.session.scalars(select(User).join(CustomerContact).where(
            CustomerContact.channel=='telegram', CustomerContact.destination==str(identity),
            CustomerContact.verified.is_(True), CustomerContact.enabled.is_(True), *visible,
        ).limit(2)))
        return matches[0] if len(matches)==1 else None
    async def remote_vless(self,uuid):
        """Resolve connection UUID through configured panels, never as a numeric user ID."""
        from app.services.remnawave import RemnawaveService
        service=RemnawaveService(self.repo,self.settings)
        panels=list(await self.session.scalars(select(RemnawaveServer).where(RemnawaveServer.status=='active')))
        found=[]
        try:
            async with asyncio.timeout(10):
                for panel in panels:
                    api=service.api(panel)
                    for start in range(0,5000,100):
                        payload=await api.users(start=start,size=100);data=payload.get('response',{})
                        items=data.get('users') if isinstance(data,dict) else None
                        if not isinstance(items,list):raise ValueError('Не удалось проверить прямой VLESS-ключ. Пришлите ссылку подписки.')
                        for row in items:
                            if isinstance(row,dict) and str(row.get('vlessUuid','')).lower()==uuid:
                                identity=row.get('id',row.get('uuid'))
                                if identity is not None:
                                    c=await self.session.scalar(select(VlessCredential).where(VlessCredential.server_id==panel.id,VlessCredential.remote_user_uuid==str(identity)))
                                    if c:found.append(('vless',c.id))
                        if len(items)<100 or isinstance(data.get('total'),int) and start+len(items)>=data['total']:break
                    else:raise ValueError('Панель слишком велика для быстрого поиска. Пришлите ссылку подписки.')
        except (TimeoutError,ValueError):raise ValueError('Не удалось полностью проверить прямой VLESS-ключ. Пришлите ссылку подписки или повторите позже.') from None
        except Exception:raise ValueError('Панель не ответила на поиск VLESS-ключа. Пришлите ссылку подписки.') from None
        return found
    async def find(self,value,actor):
        self.require(actor)
        if not value.strip() or len(value)>8192:raise ValueError('Пришлите поисковый запрос длиной до 8192 символов.')
        normalized=unicodedata.normalize('NFKC',value).strip()
        if re.fullmatch(r'[0-9]{1,19}',normalized):
            value=normalized
        kind,term=identifier(value);matches=[]
        visible = (User.status != 'deleted', ~select(AppSetting.key).where(
            AppSetting.key == ('user_archived:'+cast(User.id,String)), AppSetting.value == '1',
        ).exists())
        def add(kind,id):
            if (kind,id) not in matches:matches.append((kind,id))
        if kind=='url':
            parsed=urlsplit(term);term=urlunsplit((parsed.scheme,parsed.netloc,parsed.path,parsed.query,''))
            normalized=canonical(term)
            if not normalized:raise ValueError('Некорректная ссылка подписки.')
            tail=unquote(urlsplit(term).path.rstrip('/').rsplit('/',1)[-1])
            candidates=await self.session.scalars(select(VlessCredential).where(or_(VlessCredential.subscription_url==term,VlessCredential.short_uuid==tail)))
            for c in candidates:
                if canonical(c.subscription_url)==normalized:add('vless',c.id)
            # Some older credentials have no short_uuid. Match full URL only.
        elif kind=='vless':matches.extend(await self.remote_vless(term))
        elif kind=='telegram':
            user=await self.telegram_user(term,visible)
            if user:add('user',user.id)
        elif kind in ('key','sub'):
            if term>=2**63:raise ValueError('Некорректный номер.')
            model=AccessKey if kind=='key' else Subscription
            if await self.session.get(model,term):add(kind,term)
        else:
            if len(term)>256:raise ValueError('Для поиска по имени или ID используйте до 256 символов.')
            # Exact opaque subscription code, never an arbitrary URL substring.
            if re.fullmatch(r'[A-Za-z0-9_-]{6,128}',term):
                candidates=await self.session.scalars(select(VlessCredential).where(or_(
                    VlessCredential.short_uuid==term,
                    VlessCredential.subscription_url.endswith('/'+term,autoescape=True),
                    VlessCredential.subscription_url.endswith('/'+term+'/',autoescape=True))))
                for c in candidates:
                    tail=unquote(urlsplit(c.subscription_url or '').path.rstrip('/').rsplit('/',1)[-1])
                    if c.short_uuid==term or tail==term:add('vless',c.id)
            query=select(User)
            if re.fullmatch(r'(?i)(?:FO-|FRM-|Freeman\s+)\d{1,12}',term):
                user=await find_freeman_id(self.session,term)
                if user:add('user',user.id)
            else:
                needle=term.lstrip('@')
                if not needle:raise ValueError('Введите имя, username или Telegram ID.')
                if term.isascii() and term.isdigit() and int(term)<2**63:
                    user=await self.telegram_user(int(term),visible)
                    if user:
                        await self.repo.set_setting(f'bot_search:{actor.id}',json.dumps({
                            'items':[('user',user.id)],'expires':now().timestamp()+1800,
                        }))
                        return await self.open('user',user.id,actor)
                parts=[User.first_name.icontains(needle,autoescape=True),User.username.icontains(needle,autoescape=True),User.account_username.icontains(needle.lower(),autoescape=True),User.email.icontains(needle.lower(),autoescape=True)]
                if '@' in term and not term.startswith('@'):
                    legacy=[int(row.key.split(':',1)[1]) for row in await self.session.scalars(select(AppSetting).where(AppSetting.key.startswith('user_email:'),AppSetting.value.icontains(term,autoescape=True))) if row.key.split(':',1)[1].isdigit()]
                    if legacy:parts.append(User.id.in_(legacy))
                if term.isdigit() and int(term)<2**63:parts+=[User.id==int(term),User.telegram_id==int(term)]
                for user in await self.session.scalars(query.where(or_(*parts), *visible).order_by(User.id.desc())):
                    add('user',user.id)
                keyparts=[AccessKey.name.icontains(needle,autoescape=True)]
                if term.isdigit() and int(term)<2**63:keyparts.append(AccessKey.id==int(term))
                for key in await self.session.scalars(select(AccessKey).where(AccessKey.status!='deleted',or_(*keyparts)).order_by(AccessKey.id.desc()).limit(50)):add('key',key.id)
                # Subscription display names can differ from AccessKey.name.
                # Treat numeric names as names as well as possible identifiers.
                named=select(Subscription).join(AppSetting,AppSetting.key==('subscription_name:'+cast(Subscription.id,String))).where(AppSetting.value.icontains(needle,autoescape=True))
                for sub in await self.session.scalars(named.order_by(Subscription.id.desc())):
                    if await self.repo.setting(f'subscription_removed:{sub.id}')=='1' or await self.repo.setting(f'subscription_transferred:{sub.id}')=='1':continue
                    kid=await self.repo.setting(f'subscription_key:{sub.id}')
                    key=await self.session.get(AccessKey,int(kid)) if kid and kid.isdigit() else None
                    if key and key.status=='deleted':continue
                    add('key',key.id) if key else add('sub',sub.id)
        # Canonicalise associations so one physical subscription has one result.
        unique=[]
        for kind,id in matches:
            if kind in ('key','sub'):
                condition=VlessCredential.access_key_id==id if kind=='key' else VlessCredential.subscription_id==id
                c=await self.session.scalar(select(VlessCredential).where(condition))
                if c:kind,id='vless',c.id
            if (kind,id) not in unique:unique.append((kind,id))
        # AppSetting.value is bounded to 4096 characters in PostgreSQL. Keep
        # large result sets in small pages instead of truncating the matches.
        expires=now().timestamp()+1800
        data={'items':unique,'expires':expires}
        if len(unique)>100:
            snapshot='pages'
            for start in range(0,len(unique),100):
                await self.repo.set_setting(f'bot_search_page:{actor.id}:{snapshot}:{start//100}',json.dumps(unique[start:start+100]))
            data={'snapshot':snapshot,'count':len(unique),'expires':expires}
        await self.repo.set_setting(f'bot_search:{actor.id}',json.dumps(data))
        if len(unique)==1:return await self.open(*unique[0],actor)
        return await self.results(actor,0)
    async def results(self,actor,offset=0):
        self.require(actor);raw=await self.repo.setting(f'bot_search:{actor.id}','{}')
        try:data=json.loads(raw)
        except ValueError:data={}
        if data.get('expires',0)<now().timestamp():return Screen('Поиск устарел. Выполните его заново.',[[('🔎 Поиск','admin:input:search')]],structured=True)
        items=data.get('items',[]);total=data.get('count',len(items));offset=min(max(0,offset),max(0,(total-1)//10*10));rows=[];table=[]
        if data.get('snapshot'):
            items=json.loads(await self.repo.setting(f'bot_search_page:{actor.id}:{data["snapshot"]}:{offset//100}','[]'))
            visible=items[offset%100:offset%100+10]
        else:visible=items[offset:offset+10]
        for kind,id in visible:
            model={'user':User,'vless':VlessCredential,'key':AccessKey,'sub':Subscription}.get(kind)
            obj=await self.session.get(model,id) if model else None
            if not obj:continue
            owner=obj if kind=='user' else await self.session.get(User,obj.user_id)
            identity=(display_freeman_id(owner.freeman_id or '') or f'FO-{owner.id}') if owner else '—'
            name=owner.display_name if kind=='user' else obj.name if kind=='key' else await self.repo.setting(f'subscription_name:{obj.subscription_id if kind=="vless" else obj.id}','Подписка')
            label=(f'👤 {name[:38]} · {identity}' if kind=='user' else f'🔑 {name[:32]} · {identity}')
            rows.append([(label[:64],f'admin:search:open:{kind}:{id}:{offset}')]);table.append(['Клиент' if kind=='user' else 'Подписка',name,identity])
        if offset:rows.append([('← Предыдущие',f'admin:search:results:{offset-10}')])
        if offset+10<total:rows.append([('Следующие →',f'admin:search:results:{offset+10}')])
        rows.extend([[('🔎 Другой поиск','admin:input:search')],[('← Главное меню','home')]])
        intro=f'Найдено: {total}. По 10 записей на странице. Выберите запись.' if total else 'Ничего не найдено. Проверьте ссылку или попробуйте Freeman ID / имя клиента.'
        from app.bot.rich_copy import text_blocks
        text='🔎 <b>Результаты поиска</b>\n\n'+intro
        return Screen(text,rows,rich=InputRichMessage(blocks=text_blocks(text),skip_entity_detection=True),structured=True)
    async def open(self,kind,id,actor,offset=0):
        self.require(actor)
        from app.services.admin_key_navigation import route
        saved=json.loads(await self.repo.setting(f'bot_search:{actor.id}','{}'))
        count=saved.get('count',len(saved.get('items',[])))
        origin='h' if count==1 else f's{max(0,min(int(offset),1000000))}'
        if kind=='user':
            from app.services.admin import AdminService
            return await AdminService(self.repo,self.settings).render(route(f'admin:client:{id}',origin),actor)
        c=None;key=None;sub=None
        if kind=='vless':
            c=await self.session.get(VlessCredential,id)
            if c:key=await self.session.get(AccessKey,c.access_key_id) if c.access_key_id else None;sub=await self.session.get(Subscription,c.subscription_id)
        elif kind=='key':
            key=await self.session.get(AccessKey,id)
            if key:
                c=await self.session.scalar(select(VlessCredential).where(VlessCredential.access_key_id==id))
                if c:sub=await self.session.get(Subscription,c.subscription_id)
                else:
                    link=await self.session.scalar(select(AppSetting.key).where(AppSetting.key.startswith('subscription_key:'),AppSetting.value==str(id)).limit(1))
                    if link:sub=await self.session.get(Subscription,int(link.split(':')[1]))
        elif kind=='sub':
            sub=await self.session.get(Subscription,id)
            if sub:
                c=await self.session.scalar(select(VlessCredential).where(VlessCredential.subscription_id==id))
                kid=c.access_key_id if c else await self.repo.setting(f'subscription_key:{id}')
                if kid:key=await self.session.get(AccessKey,int(kid))
        if c and c.status!='deleted':
            from app.services.remnawave_bot import RemnawaveBot
            return await RemnawaveBot(self.repo,self.settings).render(route(f'admin:r:key:{c.id}',origin),actor)
        if not c and not key and not sub:raise ValueError('Запись больше недоступна.')
        uid=c.user_id if c else key.user_id if key else sub.user_id
        owner=await self.session.get(User,uid)
        if not owner:raise ValueError('Клиент не найден.')
        expiry=c.expires_at if c else key.expires_at if key else sub.expires_at
        status=c.status if c else key.status if key else await self.repo.setting(f'subscription_status:{sub.id}','active')
        name=await self.repo.setting(f'subscription_name:{sub.id}','') if sub else ''
        name=name or (key.name if key else 'Подписка')
        limit=c.device_limit if c else sub.device_limit if sub else None;count=None
        if c and c.status!='deleted':
            from app.services.remnawave import RemnawaveService
            from app.services.key_device_counts import device_counts
            service=RemnawaveService(self.repo,self.settings)
            try:
                panel=await service.server(c.server_id)
                count=(await device_counts([(c.id,service.api(panel),c.remote_user_uuid)]))[c.id]
            except Exception:count=None
        elif sub and not c:
            count=await self.session.scalar(select(func.count()).select_from(Device).where(Device.subscription_id==sub.id,Device.status.in_(('active','connected','online'))))
        tariff=await self.repo.tariff_by_id(sub.tariff_id) if sub and sub.tariff_id else None
        plan=f'{tariff.months} мес.' if tariff else 'Пробная' if sub and sub.kind=='trial' else 'Индивидуальная'
        if tariff:
            title=await self.repo.tariff_setting(tariff,'tariff_name','')
            if title:plan=title
        if re.match(r'^(?:vless(?:\s|·|$)|https?://)',name,re.I):name='VPN-подписка'
        device_text=f'{count} / {limit}' if count is not None and limit is not None else f'Не удалось проверить · лимит {limit}' if limit else 'Не удалось проверить'
        fallback='<b>🔑 '+escape(name)+'</b>\n\n<b>Тариф:</b> '+escape(plan)+'\n\n<b>Осталось:</b> '+escape({'deleted':'Удалена','disabled':'Приостановлена','blocked':'Приостановлена','frozen':'Заморожена'}.get(status,time_remaining(expiry)))+'\n\n<b>Действует до:</b> '+expiry_date(expiry)+'\n\n<b>Устройства:</b> '+escape(device_text)
        rows=[]
        if c and c.status!='deleted':
            rows+=[[('🔄 Продлить',f'admin:r:input:extend:{c.id}')],[('📱 Устройства'+(f' · {count}/{limit}' if count is not None else ''),f'admin:r:devices:{c.id}')],[('⚙️ Управление ключом',f'admin:r:key:{c.id}')]]
        elif sub and status!='deleted' and await self.repo.setting(f'subscription_removed:{sub.id}')!='1':rows.append([('🔄 Продлить',f'admin:input:extend:{sub.id}')])
        rows+=[[('👤 Открыть клиента',f'admin:client:{uid}')],[('🔄 Обновить карточку',f'admin:search:open:{kind}:{id}:{offset}')],[('🔎 Другой поиск','admin:input:search')],[('← Результаты поиска','admin:search:results:0')]]
        from app.bot.rich_copy import text_blocks
        from app.services.admin_key_navigation import search_context
        return search_context(Screen(fallback,rows,rich=InputRichMessage(blocks=text_blocks(fallback),skip_entity_detection=True),structured=True),origin,'admin:search:open')
    async def render(self,action,actor):
        self.require(actor);p=action.split(':')
        try:
            if p[2]=='results':return await self.results(actor,int(p[3]))
            if p[2]=='open' and p[3] in ('vless','key','sub','user'):return await self.open(p[3],int(p[4]),actor,int(p[5]) if len(p)>5 else 0)
        except (IndexError,ValueError):raise ValueError('Результат поиска недоступен. Выполните поиск заново.') from None
        raise ValueError('Действие поиска недоступно.')
