import calendar
import secrets
from html import escape
from datetime import timedelta, timezone
from urllib.parse import urlencode, quote
from sqlalchemy import select, update, func
from app.database.models import Gift, User, Tariff, AccessKey, Subscription, Payment, BalanceTransaction, ChatNotice, AdminAudit, now
from app.services.screens import Screen, money, duration
from app.services.activity import record


def month_end(months):
    current=now();month=current.month-1+months
    year=current.year+month//12;month=month%12+1
    return current.replace(year=year,month=month,day=min(current.day,calendar.monthrange(year,month)[1]))


async def cancel_legacy_notifications(session):
    from sqlalchemy import or_
    await session.execute(update(ChatNotice).where(ChatNotice.status.in_(['pending','sending']),or_(ChatNotice.dedup.startswith('gift_request:'),ChatNotice.dedup.startswith('gift.approved:'),ChatNotice.dedup.startswith('gift.received:'))).values(status='cancelled'))


class GiftService:
    def __init__(self,repo,settings):
        self.repo,self.settings,self.session=repo,settings,repo.session

    def link(self,item):
        return f'https://t.me/{self.settings.bot_username}?start=gift_{item.code}'

    def share(self,item):
        text='🎁 Вам подарок — подписка F R E E M A N!\nОставь границы позади.\n\nОткройте ссылку и нажмите «Старт», чтобы сразу получить подарок.'
        return 'url:https://t.me/share/url?'+urlencode({'url':self.link(item),'text':text},quote_via=quote)

    async def recipient(self,text,contact_id=None):
        value=text.strip().lstrip('@')
        if contact_id or value.isdigit():
            id=contact_id or int(value)
            if not 0<id<2**63:
                raise ValueError('Неверный Telegram ID.')
            user=await self.repo.user(id)
        else:
            if not value or len(value)>32:
                raise ValueError('Укажите Telegram ID, @username или контакт с Telegram ID.')
            user=await self.session.scalar(select(User).where(func.lower(User.username)==value.lower()))
            if not user:
                raise ValueError('Username не найден в боте. Получатель должен запустить бота; либо укажите его ID/контакт.')
            id=user.telegram_id
        if user and user.status=='blocked':
            raise ValueError('Получатель заблокирован.')
        return id,user

    async def owned(self,id,user):
        item=await self.session.get(Gift,int(id))
        if not item or item.sender_id!=user.id:
            raise ValueError('Подарок недоступен.')
        return item

    async def render(self,action,user):
        p=action.split(':')[1:];page=p[0]
        back=[('← Назад','g:home')]
        if page=='approve':
            item=await self.owned(p[1],user)
            return await self.render(f'g:item:{item.id}',user)
        if page=='home':
            return Screen('🎁 <b>Мои подарки</b>\n\nПодарить уже оформленную подписку можно в её карточке. Получатель получит новый ключ на оставшийся срок.',
                [[('📦 Мои подписки','subscriptions')],[('🗂 Мои подарки','g:list')],[('← Назад','subscriptions')]])
        if page=='tariffs':
            rows=[]
            for t in await self.repo.tariffs():
                if await self.repo.tariff_setting(t, 'tariff_enabled','1')=='1':
                    name=await self.repo.tariff_setting(t, 'tariff_name',duration(t.months))
                    rows.append([(f'{name} · {money(t.price)}',f'g:input:buy:id{t.id}')])
            return Screen(f'Доступный баланс: {money(await self.repo.balance(user.id))}\nВыберите тариф подарка. Если средств недостаточно, оплата появится после подключения платёжной системы.',rows+[back])
        if page=='keys':
            keys=await self.session.scalars(select(AccessKey).where(AccessKey.user_id==user.id,AccessKey.status=='active',AccessKey.mode=='mock',AccessKey.expires_at>now()).order_by(AccessKey.id.desc()).limit(30))
            rows=[[(f'#{k.id} · {k.name[:40]}',f'g:input:key:{k.id}')] for k in keys]
            return Screen('Выберите ключ для передачи. При получении старый ключ отзывается, а получателю создаётся новый на оставшийся срок. Сейчас передаются только тестовые записи.',rows+[back])
        if page=='list':
            gifts=list(await self.session.scalars(select(Gift).where(Gift.sender_id==user.id).order_by(Gift.id.desc()).limit(30)))
            names={'draft':'Черновик','ready':'Ждёт получения','claimed':'Получен','cancelled':'Отменён'}
            return Screen('Последние подарки:',[[(f'#{item.id} · {names[item.status]}',f'g:item:{item.id}')] for item in gifts]+[back])
        if page in ('item','confirm','cancelconfirm','cancel'):
            item=await self.owned(p[1],user)
            if page=='confirm':
                await self.confirm(item,user)
            elif page=='cancel':
                await self.cancel(item,user)
            recipient=await self.repo.user(item.recipient_id)
            name=f'{recipient.display_name} · @{recipient.username}' if recipient and recipient.username else recipient.display_name if recipient else 'ещё не зарегистрирован'
            names={'draft':'Черновик','ready':'Ждёт получения','claimed':'Получен','cancelled':'Отменён'}
            text=f'🎁 Подарок #{item.id}\nПолучатель: {escape(name)}\nTelegram ID: <code>{item.recipient_id}</code>\nСумма: {money(item.amount)}\nСтатус: {names[item.status]}\n'
            rows=[]
            if item.status=='draft' and item.recipient_id:
                text+='\nПроверьте ID получателя перед подтверждением. Покупка списывается с баланса; переданный ключ забирается у отправителя.'
                rows += [[('✅ Подтвердить подарок',f'g:confirm:{item.id}')]]
            elif item.status=='draft':
                text+='\nОтправьте приглашение другу. Подарок будет выдан сразу после перехода по ссылке и нажатия «Старт». Ссылка одноразовая.'
                rows += [[('👤 Выбрать пользователя',self.share(item))],[('🔗 Скопировать приглашение','copy:'+self.link(item))]]
            if item.status=='ready':
                link=self.link(item)
                text+=f'\nОтправьте ссылку получателю:\n<code>{link}</code>\nПодарок может получить только указанный Telegram ID. Срок новой подписки начинается при получении; переданный ключ сохраняет прежнюю дату окончания.'
                rows += [[('📤 Отправить другу',self.share(item))],[('🔗 Скопировать приглашение','copy:'+link)]]
            if item.status in ('draft','ready'):
                rows += [[('✅ Отменить подарок',f'g:cancel:{item.id}')]] if page=='cancelconfirm' else [[('Отменить',f'g:cancelconfirm:{item.id}')]]
            return Screen(text,rows+[back])
        if page=='input':
            if p[1]=='buy':
                t=await self.repo.tariff(p[2])
                if not t or await self.repo.tariff_setting(t, 'tariff_enabled','1')!='1':
                    raise ValueError('Тариф недоступен.')
            elif p[1]=='key':
                await self.key(int(p[2]),user.id,'active')
            elif p[1]=='subscription':
                await self.subscription_key(int(p[2]),user)
                back=[('← Назад',f'sub:{p[2]}')]
            else:
                raise ValueError('Действие недоступно.')
            item=await self.input(action,user,'',invitation=True,return_item=True)
            return Screen('🎁 <b>Подарить другу</b>\n\nВыберите чат друга и отправьте приглашение с текстом. Друг откроет ссылку, нажмёт «Старт» и сразу получит подписку. Обратного подтверждения нет. Ссылка одноразовая: отправьте её нужному другу.\n\nИли укажите @username / Telegram ID вручную. Username должен быть известен боту.',[[('👤 Выбрать пользователя',self.share(item))],[('✍️ Ввести username или ID',f'g:choose:{p[1]}:{p[2]}:manual')],back])
        if page=='choose':
            screen=await self.render(f'g:input:{p[1]}:{p[2]}',user)
            if p[3]=='pick':
                return screen
            return Screen('Отправьте @username или числовой Telegram ID друга одним сообщением.',[[('← Назад',f'g:input:{p[1]}:{p[2]}')]])
        raise ValueError('Раздел недоступен.')

    async def key(self,id,owner,status):
        item=await self.session.get(AccessKey,id)
        if not item or item.user_id!=owner or item.status!=status or item.mode!='mock' or item.expires_at.replace(tzinfo=timezone.utc)<=now():
            raise ValueError('Ключ недоступен для подарка.')
        return item

    async def subscription_key(self,sid,user):
        sub=await self.repo.subscription(user.id,sid)
        if not sub or sub.expires_at.replace(tzinfo=timezone.utc)<=now() or await self.repo.setting(f'subscription_status:{sid}','active')!='active':
            raise ValueError('Для подарка нужна действующая оформленная подписка.')
        key_id=await self.repo.setting(f'subscription_key:{sid}')
        if not key_id:
            raise ValueError('Ключ подписки ещё не выпущен.')
        if sub.kind=='trial' and not await self.repo.setting(f'demo_file:{key_id}'):
            raise ValueError('Пробную подписку подарить нельзя.')
        return await self.key(int(key_id),user.id,'active')

    async def input(self,action,user,text,contact_id=None,invitation=False,return_item=False):
        p=action.split(':')
        if len(p)!=4 or p[:2]!=['g','input']:
            raise ValueError('Действие недоступно.')
        id,recipient=(0,None) if invitation else await self.recipient(text,contact_id)
        if id==user.telegram_id:
            raise ValueError('Выберите другого получателя.')
        kind,target=p[2],p[3] if p[2]=='buy' else int(p[3])
        gift=Gift(sender_id=user.id,recipient_id=id,code=secrets.token_urlsafe(24))
        if kind=='buy':
            t=await self.repo.tariff(target)
            if not t or await self.repo.tariff_setting(t,'tariff_enabled','1')!='1':
                raise ValueError('Тариф недоступен.')
            gift.tariff_id=t.id;gift.amount=t.price
        elif kind=='key':
            key=await self.key(target,user.id,'active');gift.key_id=key.id
        elif kind=='subscription':
            key=await self.subscription_key(target,user);gift.key_id=key.id
        else:
            raise ValueError('Действие недоступно.')
        if invitation:
            existing=await self.session.scalar(select(Gift).where(Gift.sender_id==user.id,Gift.recipient_id==0,Gift.status=='draft',Gift.key_id==gift.key_id,Gift.tariff_id==gift.tariff_id,Gift.created_at>now()-timedelta(days=3)).order_by(Gift.id.desc()).limit(1))
            if existing:
                return existing if return_item else await self.render(f'g:item:{existing.id}',user)
        await self.repo.add(gift)
        if invitation:
            await self.repo.set_setting(f'gift_open:{gift.id}','1')
        if return_item:
            return gift
        return await self.render(f'g:item:{gift.id}',user)

    async def confirm(self,gift,user):
        await self.session.execute(update(User).where(User.id==user.id).values(status=User.status))
        if gift.sender_id!=user.id or await self.session.scalar(select(User.status).where(User.id==user.id))=='blocked':
            raise ValueError('Подарок недоступен.')
        await self.session.refresh(gift)
        if gift.status!='draft':
            return
        if not gift.recipient_id:
            raise ValueError('Открытое приглашение активируется самим получателем по ссылке.')
        open_invitation=await self.repo.setting(f'gift_open:{gift.id}')=='1'
        if gift.created_at.replace(tzinfo=timezone.utc)<now()-(timedelta(days=3) if open_invitation else timedelta(minutes=15)):
            raise ValueError('Черновик устарел. Создайте новый подарок.')
        recipient=await self.repo.user(gift.recipient_id)
        if recipient and recipient.status=='blocked':
            raise ValueError('Получатель заблокирован.')
        if gift.tariff_id:
            t=await self.session.get(Tariff,gift.tariff_id)
            if await self.repo.tariff_setting(t, 'tariff_enabled','1')!='1' or await self.repo.balance(user.id)<gift.amount:
                raise ValueError('Тариф выключен или недостаточно средств на балансе.')
        else:
            key=await self.key(gift.key_id,user.id,'active')
            for sub in await self.repo.subscriptions(user.id):
                if await self.repo.setting(f'subscription_key:{sub.id}')==str(key.id):
                    await self.subscription_key(sub.id,user)
            changed=await self.session.execute(update(AccessKey).where(AccessKey.id==key.id,AccessKey.user_id==user.id,AccessKey.status=='active',AccessKey.mode=='mock',AccessKey.expires_at>now()).values(status='gift_pending').execution_options(synchronize_session='fetch'))
            if not changed.rowcount:
                raise ValueError('Ключ изменился. Создайте подарок заново.')
        gift.status='ready'
        await self.session.flush()
        if gift.amount:
            await self.repo.add(BalanceTransaction(user_id=user.id,type='gift_purchase',amount=-gift.amount,description=f'Покупка подарка #{gift.id} с баланса'))
        await record(self.repo,f'gift.ready:{gift.id}',f'🎁 Подтверждён подарок #{gift.id}\nОт: {user.telegram_id}\nКому: {gift.recipient_id}\nС баланса: {money(gift.amount)}')
        # Every gift is claimed through the personal invitation, including existing users.

    async def fulfill(self,gift,recipient):
        if await self.session.scalar(select(User.status).where(User.id==recipient.id))=='blocked':
            raise ValueError('Получатель заблокирован.')
        if gift.status!='ready' or gift.recipient_id!=recipient.telegram_id:
            raise ValueError('Этот подарок предназначен другому получателю.')
        sender=await self.session.get(User,gift.sender_id)
        if gift.key_id:
            key=await self.key(gift.key_id,sender.id,'gift_pending')
            changed=await self.session.execute(update(AccessKey).where(AccessKey.id==key.id,AccessKey.user_id==sender.id,
                AccessKey.status=='gift_pending',AccessKey.mode=='mock',AccessKey.expires_at>now()).values(status='deleted').execution_options(synchronize_session='fetch'))
            if not changed.rowcount:
                raise ValueError('Ключ изменился и не может быть передан.')
            expiry=key.expires_at.replace(tzinfo=timezone.utc)
            limit=int(await self.repo.setting('device_limit',str(self.settings.device_limit)))
            old_key_id=key.id
            original_name=key.name
            for old_sub in await self.repo.subscriptions(sender.id):
                if await self.repo.setting(f'subscription_key:{old_sub.id}')==str(old_key_id):
                    limit=old_sub.device_limit
                    await self.repo.set_setting(f'subscription_status:{old_sub.id}','frozen')
                    await self.repo.set_setting(f'subscription_transferred:{old_sub.id}','1')
            key=await self.repo.add(AccessKey(user_id=recipient.id,name=original_name,expires_at=expiry))
            if await self.repo.setting(f'demo_file:{old_key_id}'):
                from app.services.demo_keys import create_demo_file
                await create_demo_file(self.repo,key)
            await self.repo.add(AdminAudit(actor_telegram_id=sender.telegram_id,
                action='gift_key_replace',target=str(old_key_id),details=f'Подарок #{gift.id}: старый ключ отозван, новый #{key.id}; MOCK'))
        else:
            t=await self.session.get(Tariff,gift.tariff_id)
            expiry=month_end(t.months);limit=t.device_limit
            key=await self.repo.add(AccessKey(user_id=recipient.id,name=f'Подарок #{gift.id}',expires_at=expiry))
        sub=await self.repo.add(Subscription(user_id=recipient.id,tariff_id=gift.tariff_id,kind='paid',expires_at=expiry,device_limit=limit))
        gift.subscription_id=sub.id;gift.status='claimed'
        await self.repo.set_setting(f'subscription_key:{sub.id}',str(key.id))
        await self.repo.set_setting(f'subscription_name:{sub.id}',f'Подарок от {sender.display_name[:40]}')
        if gift.amount:
            await self.repo.add(Payment(user_id=sender.id,subscription_id=sub.id,amount=gift.amount,status='balance_paid',provider_id=f'gift:{gift.id}'))
        # The /start response is the sole receipt. Do not queue the same event again.
        await record(self.repo,f'gift.claimed:{gift.id}',f'🎁 Подарок #{gift.id} получен\nПолучатель: {recipient.telegram_id}\nПодписка: #{sub.id}')

    async def claim(self,code,user):
        gift=await self.session.scalar(select(Gift).where(Gift.code==code))
        if not gift:
            raise ValueError('Подарок не найден.')
        await self.session.execute(update(User).where(User.id==gift.sender_id).values(status=User.status))
        async with self.session.begin_nested():
            return await self._claim(code,user)

    async def _claim(self,code,user):
        gift=await self.session.scalar(select(Gift).where(Gift.code==code))
        if not gift:
            raise ValueError('Подарок не найден или предназначен другому Telegram ID.')
        await self.session.execute(update(User).where(User.id==gift.sender_id).values(status=User.status))
        await self.session.refresh(gift)
        if gift.recipient_id==0 and gift.status=='draft' and await self.repo.setting(f'gift_open:{gift.id}')=='1':
            if gift.sender_id==user.id or user.status=='blocked' or gift.created_at.replace(tzinfo=timezone.utc)<now()-timedelta(days=3):
                raise ValueError('Это приглашение недоступно или устарело.')
            sender=await self.session.get(User,gift.sender_id)
            if sender.status=='blocked':
                raise ValueError('Подарок недоступен.')
            gift.recipient_id=user.telegram_id
            await self.session.flush()
            await self.confirm(gift,sender)
        if gift.recipient_id!=user.telegram_id:
            raise ValueError('Подарок предназначен другому Telegram ID.')
        first=gift.status=='ready'
        if first:
            await self.fulfill(gift,user)
        if gift.status!='claimed':
            raise ValueError('Подарок ещё не подтверждён или уже отменён.')
        key_id=await self.repo.setting(f'subscription_key:{gift.subscription_id}')
        sender=await self.session.get(User,gift.sender_id)
        if not first:
            return Screen('🎁 Подарок уже получен. Подписка сохранена в разделе «Мои подписки».',[[('📦 Открыть подписку',f'sub:{gift.subscription_id}')],[('← Назад','home')]])
        if key_id and await self.repo.setting(f'demo_file:{key_id}'):
            from app.services.demo_keys import key_screen
            screen=await key_screen(self.repo,self.settings,user,int(key_id),gift.subscription_id)
            screen.text=f'🎁 <b>Подарок от {escape(sender.display_name)} получен!</b>\n\n'+screen.text
            return screen
        return Screen(f'🎁 Подарок от {escape(sender.display_name)} получен!',[[('📦 Подписка',f'sub:{gift.subscription_id}')],[('← Назад','home')]])

    async def cancel(self,gift,user):
        if gift.sender_id!=user.id:
            raise ValueError('Подарок недоступен.')
        await self.session.execute(update(User).where(User.id==user.id).values(status=User.status))
        await self.session.refresh(gift)
        if gift.status not in ('draft','ready'):
            return
        if gift.status=='ready':
            if gift.amount:
                await self.repo.add(BalanceTransaction(user_id=user.id,type='gift_refund',amount=gift.amount,description=f'Отмена неполученного подарка #{gift.id}'))
            if gift.key_id:
                await self.session.execute(update(AccessKey).where(AccessKey.id==gift.key_id,AccessKey.user_id==user.id,AccessKey.status=='gift_pending').values(status='active'))
        gift.status='cancelled'
        await record(self.repo,f'gift.cancelled:{gift.id}',f'🎁 Подарок #{gift.id} отменён\nОтправитель: {user.telegram_id}')
