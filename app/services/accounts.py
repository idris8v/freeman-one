"""Shared Freeman identities and single-use verification challenges."""
import asyncio
import hashlib
import hmac
import re
import secrets
import time
import unicodedata
from datetime import timedelta, timezone
from email.message import EmailMessage
import smtplib
import ssl

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from app.database.models import User, AccountChallenge, AuthRateLimit, Referral, now

RESERVED = {'admin','administrator','root','support','freeman','freemanone','system','api','help','billing','security','moderator','www','mail','bot'}


def normalize_username(value):
    value = unicodedata.normalize('NFKC', value).strip().lower()
    if not re.fullmatch(r'[a-z][a-z0-9_]{2,31}', value) or value in RESERVED or value.startswith(('frm','freeman_')):
        raise ValueError('Логин: 3–32 символа, латинские буквы, цифры и _. Начните с буквы; служебные имена недоступны.')
    return value


def normalize_email(value):
    value = unicodedata.normalize('NFKC', value).strip().lower()
    if len(value)>254 or not re.fullmatch(r'[a-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}', value):
        raise ValueError('Проверьте адрес электронной почты')
    return value


async def ensure_account(repo, user):
    # A checkout FK placeholder is not a registered customer. An unpaid invoice
    # must remain payable, but must not allocate a public Freeman identity.
    if not user.freeman_id and not (user.telegram_id and user.telegram_id>0) and not (user.email and user.email_verified):
        from app.database.models import AccessKey,ProxyCredential,Payment
        has_service=await repo.session.scalar(select(AccessKey.id).where(AccessKey.user_id==user.id).limit(1))
        has_service=has_service or await repo.session.scalar(select(ProxyCredential.id).where(ProxyCredential.user_id==user.id).limit(1))
        paid=await repo.session.scalar(select(Payment.id).where(Payment.user_id==user.id,Payment.status.in_(('order_paid','proxy_order_paid','succeeded','balance_paid','stars_paid','manual_paid','order_processing','proxy_order_processing'))).limit(1))
        if not has_service or not paid:return user
    if not user.freeman_id:
        user.freeman_id = display_freeman_id(await repo.setting(f'freeman_id:{user.id}')) or f'FO-{user.id}'
    return user


def display_freeman_id(value):
    match=re.fullmatch(r'(?:FO-|FRM-|Freeman\s+)([0-9]+)',value or '',re.I)
    return f'FO-{int(match[1])}' if match else value

async def find_freeman_id(session, identity):
    aliases=freeman_id_aliases(identity)
    if not aliases: return None
    # Resolve permanent stored identities, not mutable usernames or guessed DB IDs.
    return await session.scalar(select(User).where(User.freeman_id.in_(aliases)))


def freeman_id_aliases(identity):
    value=identity.strip()
    match=re.fullmatch(r'(?:FO-\s*|FRM-\s*|Freeman\s+)([0-9]{1,12})','FO-'+value if value.isdigit() else value,re.I)
    if not match:return ()
    number=int(match[1])
    return (f'FO-{number}',f'FRM-{number:06d}',f'FRM-{number}',f'Freeman {number}')


async def resolve_identity(session, identity):
    identity=unicodedata.normalize('NFKC',identity).strip()
    if re.fullmatch(r'(?:FO-\s*|FRM-\s*|Freeman\s+)[0-9]{1,12}',identity,re.I):
        return await find_freeman_id(session,identity)
    if identity.isdigit():
        user=await find_freeman_id(session,identity)
        if user:return user
    if '@' in identity:
        return await session.scalar(select(User).where(User.email==identity.lower(),User.email_verified.is_(True)))
    return await session.scalar(select(User).where(User.account_username==identity.lower()))


async def throttle(session, scope, identity, limit=10, window=900):
    key=hashlib.sha256(f'{scope}:{identity}'.encode()).hexdigest()
    stamp=int(time.time())
    row=await session.get(AuthRateLimit,key)
    if row is None:
        try:
            async with session.begin_nested():
                session.add(AuthRateLimit(key=key,window_start=stamp,count=1))
                await session.flush()
            return
        except IntegrityError:
            row=await session.get(AuthRateLimit,key)
    if stamp-row.window_start>=window:
        await session.execute(update(AuthRateLimit).where(AuthRateLimit.key==key).values(window_start=stamp,count=1))
        return
    result=await session.execute(update(AuthRateLimit).where(AuthRateLimit.key==key,AuthRateLimit.count<limit).values(count=AuthRateLimit.count+1))
    if result.rowcount!=1:
        raise ValueError('Слишком много попыток. Повторите позже.')


def _digest(settings,value):
    return hmac.new(settings.encryption_key.encode(),value.encode(),hashlib.sha256).hexdigest()


async def send_email(settings, email, code):
    if not settings.smtp_host or not settings.smtp_from:
        raise RuntimeError('Отправка писем ещё не настроена. Используйте вход через Telegram.')
    def send():
        message=EmailMessage()
        message['From']=settings.smtp_from
        message['To']=email
        message['Subject']='Freeman ONE — код подтверждения'
        message.set_content(f'Ваш код подтверждения: {code}\n\nКод действует 10 минут. Никому не сообщайте его.\nЕсли вы не запрашивали код, проигнорируйте письмо.')
        context=ssl.create_default_context()
        cls=smtplib.SMTP_SSL if settings.smtp_ssl else smtplib.SMTP
        kwargs={'context':context} if settings.smtp_ssl else {}
        with cls(settings.smtp_host,settings.smtp_port,timeout=15,**kwargs) as smtp:
            if not settings.smtp_ssl:
                smtp.starttls(context=context)
            import base64
            password=base64.b64decode(settings.smtp_password_b64).decode() if settings.smtp_password_b64 else settings.smtp_password
            smtp.login(settings.smtp_username,password)
            smtp.send_message(message)
    await asyncio.to_thread(send)


async def issue_email(session,settings,email,purpose,user_id=None):
    email=normalize_email(email)
    await throttle(session,'email-send',email,5,3600)
    purposes=['login_email','login_telegram'] if purpose=='login_email' else [purpose]
    latest=await session.scalar(select(AccountChallenge).where(AccountChallenge.email==email,AccountChallenge.purpose.in_(purposes)).order_by(AccountChallenge.id.desc()))
    if latest and (now()-latest.created_at.replace(tzinfo=timezone.utc)).total_seconds()<60:
        raise ValueError('Повторный код можно запросить через минуту.')
    token=secrets.token_urlsafe(24)
    code=f'{secrets.randbelow(1_000_000):06d}'
    row=AccountChallenge(token_hash=hashlib.sha256(token.encode()).hexdigest(),purpose=purpose,email=email,user_id=user_id,
        code_hash=_digest(settings,token+':'+code),expires_at=now()+timedelta(minutes=10))
    session.add(row)
    # A replacement email code invalidates previous unused codes for this operation.
    await session.execute(update(AccountChallenge).where(AccountChallenge.email==email,
        AccountChallenge.purpose.in_(purposes),AccountChallenge.user_id==user_id,AccountChallenge.consumed.is_(False)).values(consumed=True))
    row.consumed=False
    await session.flush()
    try:
        await send_email(settings,email,code)
    except Exception as error:
        # Never log SMTP credentials, codes, or server responses containing addresses.
        row.consumed=True
        raise RuntimeError('Не удалось отправить письмо. Попробуйте позже или войдите через Telegram.') from error
    return token


async def verify_email_code(session,settings,token,code,purpose,user_id=None):
    row=await session.scalar(select(AccountChallenge).where(AccountChallenge.token_hash==hashlib.sha256(token.encode()).hexdigest(),AccountChallenge.purpose==purpose))
    if not row or row.consumed or row.attempts>=5 or row.user_id!=user_id:
        raise ValueError('Неверный код. Запросите новый, если попытки закончились.')
    if row.expires_at.replace(tzinfo=timezone.utc)<now():
        raise ValueError('Срок действия кода истёк')
    result=await session.execute(update(AccountChallenge).where(AccountChallenge.id==row.id,AccountChallenge.consumed.is_(False),AccountChallenge.attempts<5).values(attempts=AccountChallenge.attempts+1))
    if result.rowcount!=1 or not hmac.compare_digest(row.code_hash,_digest(settings,token+':'+code)):
        raise ValueError('Неверный код')
    claimed=await session.execute(update(AccountChallenge).where(AccountChallenge.id==row.id,AccountChallenge.consumed.is_(False)).values(consumed=True))
    if claimed.rowcount!=1:
        raise ValueError('Этот код уже использован.')
    return row.email


async def create_website_account(repo,email,referrer_id=None):
    if await repo.session.scalar(select(User.id).where(User.email==email)):
        raise ValueError('Email уже связан с другим аккаунтом.')
    inviter=await repo.session.get(User,referrer_id) if referrer_id else None
    user=User(email=email,email_verified=True,password_hash=None,registration_source='website_email',
        first_name='Пользователь',referrer_id=inviter.id if inviter and inviter.status=='active' else None)
    await repo.add(user)
    if user.referrer_id:
        await repo.add(Referral(inviter_id=user.referrer_id,invitee_id=user.id))
    return user


async def issue_telegram_link(session,user_id):
    token=secrets.token_urlsafe(24)
    session.add(AccountChallenge(token_hash=hashlib.sha256(token.encode()).hexdigest(),purpose='telegram_link',user_id=user_id,expires_at=now()+timedelta(minutes=10)))
    await session.flush()
    return token


async def send_telegram(settings,telegram_id,code):
    from aiogram import Bot
    async with Bot(settings.bot_token) as bot:
        await bot.send_message(telegram_id,
            f'Freeman ONE — код входа: {code}\n\nКод действует 10 минут. '
            'Никому не сообщайте его. Если вы не запрашивали вход, проигнорируйте сообщение.')


async def issue_telegram_otp(session,settings,user,*,also_email=False):
    await throttle(session,'telegram-send',str(user.id),5,3600)
    latest=await session.scalar(select(AccountChallenge).where(
        AccountChallenge.user_id==user.id,AccountChallenge.purpose.in_(['login_email','login_telegram'])).order_by(AccountChallenge.id.desc()))
    if latest and (now()-latest.created_at.replace(tzinfo=timezone.utc)).total_seconds()<60:
        raise ValueError('Повторный код можно запросить через минуту.')
    if also_email:
        await throttle(session,'email-send',user.email,5,3600)
    await session.execute(update(AccountChallenge).where(AccountChallenge.user_id==user.id,
        AccountChallenge.purpose.in_(['login_email','login_telegram']),AccountChallenge.consumed.is_(False)).values(consumed=True))
    token=secrets.token_urlsafe(24)
    code=f'{secrets.randbelow(1_000_000):06d}'
    session.add(AccountChallenge(token_hash=sha256_token(token),purpose='login_telegram',user_id=user.id,email=user.email if also_email else None,
        code_hash=_digest(settings,token+':'+code),expires_at=now()+timedelta(minutes=10)))
    await session.flush()
    delivered=[]
    try:
        await send_telegram(settings,user.telegram_id,code)
        delivered.append('telegram')
    except Exception:
        pass
    if also_email:
        try:
            await send_email(settings,user.email,code)
            delivered.append('email')
        except Exception:
            pass
    if not delivered:
        raise RuntimeError('Не удалось доставить код. Попробуйте позже или откройте бота и нажмите «Старт».')
    if also_email:
        return token,delivered
    return token


def sha256_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


async def link_telegram(repo,token,telegram_user):
    row=await repo.session.scalar(select(AccountChallenge).where(AccountChallenge.token_hash==hashlib.sha256(token.encode()).hexdigest(),AccountChallenge.purpose=='telegram_link'))
    if not row or row.consumed or row.expires_at.replace(tzinfo=timezone.utc)<now():
        raise ValueError('Ссылка привязки истекла. Откройте настройки на сайте и повторите.')
    user=await repo.session.get(User,row.user_id)
    existing=await repo.user(telegram_user.id)
    if not user or user.status!='active' or (existing and existing.id!=user.id) or (user.telegram_id and user.telegram_id!=telegram_user.id):
        raise ValueError('Telegram уже связан с другим аккаунтом. Автоматический перенос недоступен.')
    claim=await repo.session.execute(update(AccountChallenge).where(AccountChallenge.id==row.id,AccountChallenge.consumed.is_(False)).values(consumed=True))
    if claim.rowcount!=1:
        raise ValueError('Ссылка уже использована.')
    user.telegram_id=telegram_user.id
    user.username=telegram_user.username
    if user.first_name=='Пользователь': user.first_name=telegram_user.first_name
    await repo.session.flush()
    return user
