"""Durable transactional mail. Credentials and message bodies are never logged.

SMTP provides at-least-once delivery: an interrupted acknowledgement can cause
one duplicate. Stable Message-ID and atomic claims reduce that risk.
"""
import asyncio
import base64
import smtplib
import ssl
from datetime import timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError
from app.database.models import EmailDelivery, User, Subscription, AccessKey, Payment, VpnCredential, VlessCredential, now
from app.database.repositories import SQLRepository
from app.services.reminder_policy import stage, current, legacy_covers

PAID = ('succeeded', 'order_paid', 'stars_paid', 'balance_paid')

def expiry(value):
    return value.replace(tzinfo=timezone.utc).isoformat()

def ready(settings):
    return bool(settings.smtp_host and settings.smtp_from and settings.smtp_username and (settings.smtp_password or settings.smtp_password_b64))

async def enqueue(repo, **values):
    if await repo.session.scalar(select(EmailDelivery.id).where(EmailDelivery.dedup==values['dedup'])):
        return
    try:
        async with repo.session.begin_nested():
            repo.session.add(EmailDelivery(**values))
            await repo.session.flush()
    except IntegrityError:
        pass  # Another worker inserted the same event.


async def administrator(repo, settings, user):
    telegram_id = user.telegram_id
    return bool(telegram_id and (telegram_id in settings.owner_ids()
                or await repo.setting(f'staff_role:{telegram_id}') == 'administrator'))


async def prepare_mail(repo):
    from app.database.models import GuestOrder
    await repo.session.execute(update(GuestOrder).where(GuestOrder.expires_at<=now(),GuestOrder.secret!='').values(secret='').execution_options(synchronize_session=False))
    # Rollout must not send a purchase receipt for every historical payment.
    baseline=await repo.setting('email_purchase_baseline',None)
    if baseline is None:
        baseline=str(await repo.session.scalar(select(func.max(Payment.id))) or 0)
        await repo.set_setting('email_purchase_baseline',baseline)
    if await repo.setting('email_purchase_history_ready')!='1':
        historical=list(await repo.session.scalars(select(Payment).where(Payment.id<=int(baseline),Payment.status.in_(PAID),Payment.subscription_id.is_not(None))))
        for payment in historical:
            sub=await repo.session.get(Subscription,payment.subscription_id)
            if sub:
                await enqueue(repo,dedup=f'purchase:{payment.id}',user_id=payment.user_id,subscription_id=sub.id,payment_id=payment.id,kind='historical',expiry=expiry(sub.expires_at),status='skipped')
        await repo.set_setting('email_purchase_history_ready','1')
    # A payment started before rollout can still succeed afterwards.
    queued=select(EmailDelivery.id).where(EmailDelivery.payment_id==Payment.id).exists()
    payments=list(await repo.session.scalars(select(Payment).where(Payment.status.in_(PAID),Payment.subscription_id.is_not(None),~queued)))
    for payment in payments:
        sub=await repo.session.get(Subscription,payment.subscription_id)
        user=await repo.session.get(User,payment.user_id)
        if not sub or sub.user_id!=payment.user_id or not user or not user.email_verified or not user.email:
            continue
        await enqueue(repo,dedup=f'purchase:{payment.id}',user_id=user.id,subscription_id=sub.id,payment_id=payment.id,kind='purchase',expiry=expiry(sub.expires_at))
    if await repo.setting('reminders_enabled','1')!='1':
        return
    from app.config.settings import Settings
    settings = Settings()
    subs=list(await repo.session.scalars(select(Subscription).where(Subscription.expires_at>now(),Subscription.expires_at<=now()+timedelta(days=3),Subscription.kind.in_(('paid','admin')))))
    for sub in subs:
        user=await repo.session.get(User,sub.user_id)
        if not user or not user.email_verified or not user.email or await administrator(repo,settings,user):
            continue
        due=stage(sub.expires_at,now())
        if due is None:continue
        if await repo.setting(f'notifications:{user.id}','1')!='1' or user.status=='blocked':continue
        legacy_key=f'expiry:{sub.id}:{expiry(sub.expires_at)}'
        legacy=await repo.session.scalar(select(EmailDelivery).where(EmailDelivery.dedup==legacy_key))
        if legacy_covers(legacy,due,sub.expires_at):continue
        if due==1 and legacy and legacy.status in ('pending','sending'):legacy.status='cancelled'
        await enqueue(repo,dedup=legacy_key+f':d{due}',user_id=user.id,subscription_id=sub.id,kind='expiry',expiry=expiry(sub.expires_at))

async def mail_content(repo, settings, job):
    user=await repo.session.get(User,job.user_id)
    if job.kind=='offer':
        from app.database.models import OfferDelivery,PersonalOffer
        from app.services.offers import offer_status,offer_link
        delivery=await repo.session.scalar(select(OfferDelivery).where(OfferDelivery.email_job_id==job.id))
        offer=await repo.session.get(PersonalOffer,delivery.offer_id) if delivery else None
        if not offer or offer_status(offer)!='active' or not user or user.status=='blocked' or await repo.setting(f'notifications:{user.id}','1')!='1':return None
        from app.services.proxy_control import notice_current
        if not await notice_current(repo,offer):return None
        if delivery.contact_id=='email:account' and (not user.email_verified or user.email!=delivery.destination):return None
        message=EmailMessage();message['From']=settings.smtp_from;message['To']=delivery.destination;message['Subject']=offer.title
        message['Message-ID']=f'<freeman-offer-{job.id}@{settings.smtp_from.split("@")[-1]}>'
        message.set_content(offer.message+'\n\n'+offer_link(settings,offer))
        return message
    sub=await repo.session.get(Subscription,job.subscription_id)
    if not user or user.status=='blocked' or not user.email_verified or not user.email or not sub or sub.user_id!=user.id:
        return None
    if await repo.setting(f'subscription_removed:{sub.id}')=='1' or await repo.setting(f'subscription_transferred:{sub.id}')=='1':
        return None
    kid=await repo.setting(f'subscription_key:{sub.id}')
    key=await repo.session.get(AccessKey,int(kid)) if kid else None
    if not key and job.kind=='purchase':
        raise RuntimeError('connection_not_ready')
    if not key or key.user_id!=user.id or key.status=='deleted':
        return None
    if job.kind=='expiry':
        if await administrator(repo,settings,user):
            return None
        if not current(job.dedup,sub.expires_at,now()) or expiry(sub.expires_at)!=job.expiry or sub.expires_at.replace(tzinfo=timezone.utc)<=now() or key.status!='active' or await repo.setting(f'subscription_status:{sub.id}','active')!='active' or await repo.setting(f'notifications:{user.id}','1')!='1' or await repo.setting('reminders_enabled','1')!='1':
            return None
    else:
        payment=await repo.session.get(Payment,job.payment_id)
        if not payment or payment.status not in PAID or payment.user_id!=user.id or payment.subscription_id!=sub.id:
            return None
    base=settings.web_base_url.rstrip('/')
    date=sub.expires_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y в %H:%M (МСК)')
    renew=f'{base}/?renew_key={key.id}'
    if job.kind=='expiry':
        subject=f'Freeman ONE — заканчивается ключ «{key.name}»'
        body=f'Здравствуйте!\n\nСрок ключа «{key.name}» заканчивается {date}.\n\nПродлить этот ключ:\n{renew}\n\nПосле входа откроется продление выбранного ключа.\nFreeman ONE'
    else:
        vpn=await repo.session.scalar(select(VpnCredential).where(VpnCredential.access_key_id==key.id,VpnCredential.user_id==user.id,VpnCredential.status=='active'))
        vless=await repo.session.scalar(select(VlessCredential).where(VlessCredential.access_key_id==key.id,VlessCredential.user_id==user.id,VlessCredential.status=='active'))
        value=None
        if vpn and vpn.config:
            from app.services.vpn import VpnService
            value=VpnService(repo,settings).decrypt(vpn.config)
        elif vless and vless.subscription_url:
            value=vless.subscription_url
        if not value:
            raise RuntimeError('connection_not_ready')  # Retry after provisioning, never send a mock key.
        subject=f'Freeman ONE — ваш ключ «{key.name}»'
        body=f'Здравствуйте!\n\nПокупка успешно завершена.\nКлюч: «{key.name}»\nДействует до {date}.\n\nДанные подключения:\n{value}\n\nПошаговая установка этого ключа:\n{base}/?install_key={key.id}\n\nПродлить:\n{renew}\n\nНе передавайте данные подключения посторонним.\nFreeman ONE'
    if job.kind=='purchase':
        from app.web.guest_purchase import GuestOrder
        guest=await repo.session.scalar(select(GuestOrder).where(GuestOrder.payment_id==job.payment_id,GuestOrder.user_id==user.id))
        if guest:
            body += '\n\nУстановка: откройте приложение для VLESS, добавьте подписку по ссылке и вставьте данные подключения выше. При продлении обновите подписку в приложении.\n'
            body += f'\nИнструкция с выбором устройства:\n{base}/connect-guide?receipt={guest.id}\n'
            body += f'Продление без входа: {base}/buy/renew\n'
            if not guest.renewal_sid and guest.secret and guest.expires_at.replace(tzinfo=timezone.utc)>now():
                from cryptography.fernet import Fernet
                from app.services.accounts import display_freeman_id
                password=Fernet(settings.encryption_key.encode()).decrypt(guest.secret.encode()).decode()
                body += f'\nВажно — сохраните данные для входа:\nЛогин: {display_freeman_id(user.freeman_id)}\nПароль: {password}\nВход: {base}/buy/login\nПароль можно заменить после входа.\n'
    message=EmailMessage()
    message['From']=settings.smtp_from
    message['To']=user.email
    message['Subject']=subject
    message['Message-ID']=f'<freeman-mail-{job.id}@{settings.smtp_from.split("@")[-1]}>'
    message.set_content(body)
    return message

async def send_message(settings,message):
    def send():
        context=ssl.create_default_context()
        cls=smtplib.SMTP_SSL if settings.smtp_ssl else smtplib.SMTP
        args={'context':context} if settings.smtp_ssl else {}
        with cls(settings.smtp_host,settings.smtp_port,timeout=20,**args) as smtp:
            if not settings.smtp_ssl:
                smtp.starttls(context=context)
            password=base64.b64decode(settings.smtp_password_b64).decode() if settings.smtp_password_b64 else settings.smtp_password
            smtp.login(settings.smtp_username,password)
            smtp.send_message(message)
    await asyncio.to_thread(send)

class EmailWorker:
    def __init__(self,database,settings):
        self.database,self.settings=database,settings

    async def run(self):
        import logging
        while True:
            try:
                await self.once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.getLogger(__name__).warning('Email worker failed: %s',type(error).__name__)
            await asyncio.sleep(5)

    async def once(self):
        if not ready(self.settings):
            return False
        async with self.database.sessions.begin() as session:
            await session.execute(update(EmailDelivery).where(EmailDelivery.status=='sending',EmailDelivery.available_at<=now()).values(status='pending'))
            job=await session.scalar(select(EmailDelivery).where(EmailDelivery.status=='pending',EmailDelivery.available_at<=now()).order_by(EmailDelivery.id).limit(1))
            if not job:
                return False
            result=await session.execute(update(EmailDelivery).where(EmailDelivery.id==job.id,EmailDelivery.status=='pending').values(status='sending',attempts=EmailDelivery.attempts+1,available_at=now()+timedelta(minutes=5)))
            if not result.rowcount:
                return False
            job_id=job.id
        # SMTP outside the DB transaction: slow mail must not lock purchases.
        status,delay='sent',0
        try:
            async with self.database.sessions() as session:
                job=await session.get(EmailDelivery,job_id)
                attempts=job.attempts
                message=await mail_content(SQLRepository(session),self.settings,job)
            if message is None:
                status='cancelled'
            else:
                await send_message(self.settings,message)
        except Exception:
            status='failed' if attempts>=12 else 'pending'
            delay=min(3600,30*2**min(attempts,7))
        async with self.database.sessions.begin() as session:
            await session.execute(update(EmailDelivery).where(EmailDelivery.id==job_id,EmailDelivery.status=='sending').values(status=status,available_at=now()+timedelta(seconds=delay),sent_at=now() if status=='sent' else None))
        return True
