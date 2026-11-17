"""Durable at-least-once delivery. Never logs messages or payment requisites."""
import asyncio
import logging
import json,hashlib
from datetime import timedelta, timezone
from sqlalchemy import select, update, func
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter, TelegramNetworkError
from app.database.models import User, Subscription, Delivery, Campaign, now
from app.database.repositories import SQLRepository
from app.services.reminder_policy import stage, current, legacy_covers


async def renewal_hint(repo, settings, user, sub):
    from app.services.recurring_cards import renewal_status
    from app.services.pricing import quote
    from app.services.screens import money

    status = await renewal_status(repo, settings, user, sub)
    if not status['enabled'] or not status['master']:
        return True, 'Можно подключить автопродление.', False
    if status['reason']:
        return True, status['reason'], True
    if await repo.setting(f'auto_renew_cycle:{sub.id}', 'monthly') == 'daily':
        price = 500
    else:
        tariff = await repo.auto_tariff(sub.id)
        if not tariff or tariff.device_limit != sub.device_limit or await repo.tariff_setting(tariff, 'tariff_enabled', '1') != '1' or await repo.tariff_setting(tariff, 'tariff_deleted', '0') == '1':
            return True, 'Тариф автопродления недоступен. Выберите новый.', True
        price = (await quote(repo, user.id, tariff, sub.id))[0]
    if status['source'] == 'balance':
        shortage = price - await repo.balance(user.id)
        if shortage > 0:
            return True, f'Автопродление с баланса · {money(price)}. Не хватает {money(shortage)}.', True
        return False, f'Автопродление с баланса · {money(price)}. Сейчас средств хватает.', True
    return False, f'Автопродление с сохранённой карты · {money(price)}.', True


async def expiry_screen(repo,user,sub,settings=None):
    from html import escape
    from math import ceil
    from urllib.parse import urlsplit
    from app.database.models import AccessKey, VlessCredential
    from app.services.admin_client_grant import rich
    from app.services.subscription_labels import current as key_label

    credential=await repo.session.scalar(select(VlessCredential).where(
        VlessCredential.subscription_id==sub.id,
        VlessCredential.user_id==user.id,
        VlessCredential.status=='active'))
    key_id=await repo.setting(f'subscription_key:{sub.id}')
    key=await repo.session.get(AccessKey,credential.access_key_id if credential else int(key_id)) if credential or key_id.isdigit() else None
    if key and key.user_id!=user.id:
        key=None
    name,_=await key_label(repo,sub,key)
    remaining=max(1,ceil((sub.expires_at.replace(tzinfo=timezone.utc)-now()).total_seconds()/86400))
    from app.config.settings import Settings
    needs_action, hint, configured = await renewal_hint(repo, settings or Settings(), user, sub)
    body=f'⏳ <b>Подписка скоро закончится</b>\n\n🔑 <b>Ключ:</b> {escape(name)}\n📅 <b>Осталось:</b> {remaining} дн.'
    if key and key.status=='active' and credential:
        link=credential.subscription_url
        try:
            parts=urlsplit(link)
            if parts.scheme=='https' and parts.hostname and not parts.username and not parts.password:
                body+=f'\n\n<b>Ссылка на подписку</b>\n<code>{escape(link)}</code>'
        except ValueError:
            pass
    body+=f'\n\n💡 {escape(hint)}'
    target=f'renew:{sub.id}:quick'
    rows=[[('🔄 Продлить подписку',target)]]
    if needs_action:
        label = '⚙️ Проверить автопродление' if configured else '🔁 Подключить автопродление'
        rows.append([(label, f'autorenew:{sub.id}')])
    screen=rich(body,rows)
    screen.button_options={target:{'style':'success'}}
    return screen


class DispatchWorker:
    def __init__(self, database, bot, settings=None):
        self.database, self.bot = database, bot
        self.settings = settings
        self.pause_until = 0
        self.next_winback_prepare = 0
        self.next_infra_prepare = 0
        from app.services.notices import NoticeWorker
        self.notices = NoticeWorker(database, bot, settings)

    async def prepare(self):
        from app.config.settings import Settings
        from app.services.topups import reconcile
        settings = self.settings or Settings()
        await reconcile(self.database,settings)
        from app.services.recurring_cards import prepare_cards
        await prepare_cards(self.database,settings)
        # VPN calls can take seconds. Hold only the affected customer's lock
        # during these phases, never the entire customer table.
        async with self.database.sessions.begin() as session:
            from app.services.client_keys import prepare_client_thaws
            await prepare_client_thaws(SQLRepository(session),settings)
        async with self.database.sessions.begin() as session:
            from app.services.billing import prepare_autorenew
            await prepare_autorenew(SQLRepository(session),settings)
        clock = asyncio.get_running_loop()
        if clock.time() >= self.next_winback_prepare:
            from app.services.winback import prepare as prepare_winback
            async with self.database.sessions.begin() as session:
                await prepare_winback(SQLRepository(session), settings)
            self.next_winback_prepare = clock.time() + 900
        async with self.database.sessions.begin() as session:
            # Keep reward/withdrawal serialization in a short, DB-only phase.
            await session.execute(update(User).values(status=User.status))
            from app.services.referral_accounts import prepare_rewards
            await prepare_rewards(SQLRepository(session))
        async with self.database.sessions.begin() as session:
            repo=SQLRepository(session)
            from app.services.email_delivery import prepare_mail
            await prepare_mail(repo)
            from app.services.trial_offer import convertible, prepare_conversion
            await prepare_conversion(repo, settings)
            from app.services.partner_ledger import prepare_reminders
            await prepare_reminders(repo)
            if await repo.setting('reminders_enabled','1')=='1':
                hours=72
                subs=await session.scalars(select(Subscription).where(Subscription.expires_at>now(),Subscription.expires_at<=now()+timedelta(hours=hours)))
                for sub in subs:
                    user=await session.get(User,sub.user_id)
                    if user.status=='blocked' or await repo.setting(f'notifications:{user.id}','1')!='1' or await repo.setting(f'subscription_status:{sub.id}','active')!='active' or await repo.setting(f'subscription_removed:{sub.id}')=='1' or await repo.setting(f'subscription_transferred:{sub.id}')=='1':
                        continue
                    if sub.kind=='trial' and (await repo.setting('reminder_trials','0')!='1'
                                              or await convertible(repo,settings,sub,user)):
                        continue
                    expiry=sub.expires_at.replace(tzinfo=timezone.utc).isoformat()
                    legacy_key=f'expiry:{sub.id}:{expiry}'
                    due=stage(sub.expires_at,now())
                    if due is None:continue
                    legacy=await session.scalar(select(Delivery).where(Delivery.dedup==legacy_key))
                    if legacy_covers(legacy,due,sub.expires_at):continue
                    if due==1 and legacy and legacy.status in ('pending','sending'):legacy.status='skipped'
                    key=legacy_key+f':d{due}'
                    from app.services.screens import ScreenService
                    name=await ScreenService(repo,settings,None,None).subscription_list_label(sub)
                    enabled=await repo.setting(f'auto_renew:{sub.id}','0')=='1' and await repo.setting(f'auto_master:{user.id}','1')=='1'
                    remaining=max(1,__import__('math').ceil((sub.expires_at.replace(tzinfo=timezone.utc)-now()).total_seconds()/86400))
                    text=f'⏳ Подписка скоро закончится\n\n🔑 {name}\n📅 Осталось: {remaining} дн.\n\nПродлите подписку, чтобы подключение не прервалось.'
                    text+=('\nАвтопродление включено. Успех зависит от доступных средств.' if enabled else '\n💡 Можно подключить автопродление.')
                    notice_key=f'web_notice:{user.id}:'+hashlib.sha256(key.encode()).hexdigest()
                    if not await repo.setting(notice_key):await repo.set_setting(notice_key,json.dumps(dict(subscription_id=sub.id,expiry=expiry,text=text,renewal_enabled=enabled,created_at=now().isoformat()),ensure_ascii=False))
                    if settings.is_owner(user.telegram_id):
                        continue
                    if due == 1 and not (await renewal_hint(repo, settings, user, sub))[0]:
                        continue
                    if await session.scalar(select(Delivery.id).where(Delivery.dedup==key)):
                        continue
                    await repo.add(Delivery(dedup=key,user_id=user.id,subscription_id=sub.id,expiry=expiry,
                        text=text))
            campaigns=await session.scalars(select(Campaign).where(Campaign.status=='queued'))
            for campaign in campaigns:
                pending=await session.scalar(select(func.count()).select_from(Delivery).where(Delivery.campaign_id==campaign.id,Delivery.status.in_(['pending','sending'])))
                if not pending:
                    campaign.status='done'
            from app.services.activity import prepare_history_maintenance
            await prepare_history_maintenance(repo)

    async def once(self):
        if asyncio.get_running_loop().time() < self.pause_until:
            return False
        async with self.database.sessions.begin() as session:
            repo=SQLRepository(session)
            await session.execute(update(Delivery).where(Delivery.status=='sending',Delivery.available_at<=now()).values(status='pending'))
            job=await session.scalar(select(Delivery).where(Delivery.status=='pending',Delivery.available_at<=now()).order_by(Delivery.id).limit(1))
            if not job:
                return False
            claimed=await session.execute(update(Delivery).where(Delivery.id==job.id,Delivery.status=='pending').values(status='sending',attempts=Delivery.attempts+1,available_at=now()+timedelta(minutes=5)))
            if not claimed.rowcount:
                return False
            user=await session.get(User,job.user_id)
            valid=bool(user and user.status!='blocked' and (user.telegram_id or (job.photo and job.photo.startswith('offer:'))))
            if job.campaign_id:
                campaign=await session.get(Campaign,job.campaign_id)
                valid=valid and campaign.status=='queued'
                if valid:
                    from app.services.winback import AUDIENCE, eligible
                    if campaign.audience==AUDIENCE:
                        from app.config.settings import Settings
                        valid=await eligible(repo,self.settings or Settings(),user)
            reminder=None
            if job.subscription_id:
                from app.config.settings import Settings
                settings=self.settings or Settings()
                valid=valid and not settings.is_owner(user.telegram_id)
                sub=await session.get(Subscription,job.subscription_id)
                valid=valid and bool(sub and sub.user_id==user.id) and sub.expires_at.replace(tzinfo=timezone.utc).isoformat()==job.expiry and sub.expires_at.replace(tzinfo=timezone.utc)>now() and current(job.dedup,sub.expires_at,now())
                valid=valid and await repo.setting('reminders_enabled','1')=='1' and await repo.setting(f'notifications:{user.id}','1')=='1' and await repo.setting(f'subscription_status:{job.subscription_id}','active')=='active' and await repo.setting(f'subscription_removed:{job.subscription_id}')!='1' and await repo.setting(f'subscription_transferred:{job.subscription_id}')!='1'
                if valid and stage(sub.expires_at, now()) == 1:
                    valid = (await renewal_hint(repo, settings, user, sub))[0]
                if valid:
                    reminder=await expiry_screen(repo,user,sub,settings)
            id,telegram_id,text,photo,attempts=job.id,user.telegram_id if user else None,job.text,job.photo,job.attempts
            from app.services.campaign_content import load
            campaign_content=await load(repo,job.campaign_id) if job.campaign_id else None
            assistant_rich=bool(job.campaign_id and await repo.setting(f'assistant_campaign_rich:{job.campaign_id}')=='1')
            offer_data=None
            offer_button_label='Открыть предложение'
            if photo and photo.startswith('offer:'):
                from app.database.models import PersonalOffer,OfferDelivery
                from app.services.offers import offer_status,offer_link
                offer=await session.get(PersonalOffer,int(photo.split(':')[1]))
                destination=await session.scalar(select(OfferDelivery).where(OfferDelivery.telegram_job_id==id))
                valid=valid and bool(offer and offer_status(offer)=='active' and destination and await repo.setting(f'notifications:{user.id}','1')=='1')
                if destination:
                    try:telegram_id=int(destination.destination)
                    except (ValueError,TypeError):valid=False
                    # A queued offer must not follow a detached/replaced contact.
                    from app.services.offers import contacts
                    from app.config.settings import Settings
                    available=await contacts(repo,self.settings or Settings(),user) if user else []
                    valid=valid and any(c['available'] and c['id']==destination.contact_id and c['destination']==destination.destination for c in available)
                if offer and offer.target=='message':offer_button_label='Открыть Freeman ONE'
                if offer:
                    from app.services.proxy_control import notice_current
                    valid=valid and await notice_current(repo,offer)
                    offer_data=offer_link(self.settings,offer) if self.settings else None
                photo=None
            post=None
            if photo and photo.startswith('post:'):
                from app.web.posts import load_json
                post=await load_json(repo,'web_post_delivery:'+str(job.campaign_id))
                valid=valid and bool(post)
            if not valid:
                await session.execute(update(Delivery).where(Delivery.id==id).values(status='skipped'))
                return True
        status,delay='sent',0
        try:
            if campaign_content:
                from app.services.campaign_content import send
                from app.services.campaign_buttons import data as buttons,rows,options
                async with self.database.sessions() as session:
                    value=await buttons(SQLRepository(session),job.campaign_id)
                campaign_content=dict(campaign_content,buttons=rows(value),button_options=options(value))
                await send(self.bot,telegram_id,campaign_content)
            elif offer_data:
                from aiogram.types import InlineKeyboardMarkup,InlineKeyboardButton
                markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=offer_button_label,url=offer_data)]])
                if assistant_rich:
                    from aiogram.types import InputRichMessage
                    await self.bot.send_rich_message(telegram_id,InputRichMessage(markdown=text,skip_entity_detection=False),reply_markup=markup)
                else:await self.bot.send_message(telegram_id,text,parse_mode=None,reply_markup=markup)
            elif post:
                await self.send_post(id,telegram_id,post,text)
            elif photo:
                await self.bot.send_photo(telegram_id,photo,caption=text,parse_mode=None)
            elif reminder:
                from app.bot.handlers.navigation import navigation_rich
                from app.bot.keyboards import keyboard
                markup=keyboard(reminder.rows,reminder.button_options)
                try:
                    await self.bot.send_rich_message(telegram_id,navigation_rich(reminder),reply_markup=markup)
                except TelegramBadRequest:
                    await self.bot.send_message(telegram_id,reminder.text,parse_mode='HTML',reply_markup=markup)
            else:
                await self.bot.send_message(telegram_id,text,parse_mode=None)
        except TelegramRetryAfter as error:
            status,delay='pending',error.retry_after+1
            self.pause_until=asyncio.get_running_loop().time()+delay
        except (TelegramForbiddenError,TelegramBadRequest):
            status='failed'
        except (TelegramNetworkError,TimeoutError):
            status,delay=('failed',0) if attempts>=5 else ('pending',min(3600,2**attempts*10))
        async with self.database.sessions.begin() as session:
            await session.execute(update(Delivery).where(Delivery.id==id,Delivery.status=='sending').values(status=status,available_at=now()+timedelta(seconds=delay)))
        return True

    async def prepare_periodically(self):
        loop = asyncio.get_running_loop()
        next_prepare = 0
        while True:
            try:
                if loop.time() >= self.next_infra_prepare:
                    from app.config.settings import Settings
                    from app.services.server_alerts import INTERVAL, prepare as prepare_infra
                    self.next_infra_prepare = loop.time() + INTERVAL
                    try:
                        await prepare_infra(self.database, self.settings or Settings())
                    except Exception as error:
                        logging.getLogger(__name__).warning('Infrastructure check failed: %s',type(error).__name__)
                if loop.time()>=next_prepare:
                    await self.prepare()
                    next_prepare=loop.time()+60
            except Exception as error:
                logging.getLogger(__name__).warning('Background preparation failed: %s', type(error).__name__)
            await asyncio.sleep(5)

    async def run(self, *, telegram_ready=None):
        from app.services.activity import prepare_history

        preparation = asyncio.create_task(self.prepare_periodically())
        loop = asyncio.get_running_loop()
        next_history = 0
        try:
            while True:
                if loop.time() >= next_history:
                    next_history = loop.time() + 3
                    try:
                        async with self.database.sessions.begin() as session:
                            await prepare_history(SQLRepository(session))
                    except Exception as error:
                        logging.getLogger(__name__).warning('History preparation failed: %s', type(error).__name__)
                try:
                    if (telegram_ready is None or telegram_ready.is_set()) and loop.time() >= max(self.pause_until, self.notices.pause_until):
                        try:
                            await self.notices.forum.close_once()
                        except TelegramRetryAfter as error:
                            self.pause_until = self.notices.pause_until = loop.time()+error.retry_after+1
                        if not await self.notices.once():
                            await self.once()
                except Exception as error:
                    logging.getLogger(__name__).warning('Background delivery failed: %s', type(error).__name__)
                    await asyncio.sleep(5)
                # Global one message/second respects both bulk and individual chat limits.
                await asyncio.sleep(1.1)

        finally:
            preparation.cancel()
            await asyncio.gather(preparation, return_exceptions=True)

    async def send_post(self,delivery_id,telegram_id,post,text):
        import html
        from pathlib import Path
        from aiogram.types import FSInputFile,InputMediaPhoto,InputMediaVideo,InlineKeyboardMarkup,InlineKeyboardButton
        from app.web.posts import STORAGE
        from app.config.settings import Settings
        from app.services.publication_urls import publication_url
        files=post.get('media',[])
        media=[x for x in files if Path(x).suffix in ('.jpg','.png','.webp','.mp4')]
        docs=[x for x in files if x not in media]
        steps=[]
        if media:steps.append(('album',media))
        steps.extend(('document',[x]) for x in docs)
        steps.append(('text',[]))
        for index,(kind,names) in enumerate(steps):
            marker=f'web_post_step:{delivery_id}:{index}'
            async with self.database.sessions() as session:
                if await SQLRepository(session).setting(marker):continue
            if kind=='album':
                album=[(InputMediaVideo if Path(x).suffix=='.mp4' else InputMediaPhoto)(media=FSInputFile(STORAGE/x)) for x in names]
                if len(album)>1:await self.bot.send_media_group(telegram_id,album)
                elif Path(names[0]).suffix=='.mp4':await self.bot.send_video(telegram_id,FSInputFile(STORAGE/names[0]))
                else:await self.bot.send_photo(telegram_id,FSInputFile(STORAGE/names[0]))
            elif kind=='document':await self.bot.send_document(telegram_id,FSInputFile(STORAGE/names[0]))
            else:
                await self.bot.send_message(telegram_id,'<b>'+html.escape(post['title'])+'</b>\n\n'+post['body'],parse_mode='HTML',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Открыть публикацию',url=publication_url(self.settings or Settings(),post['id']))]]))
            async with self.database.sessions.begin() as session:await SQLRepository(session).set_setting(marker,'1')
            if index<len(steps)-1:await asyncio.sleep(1.1)
