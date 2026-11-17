from hashlib import sha256
from ipaddress import ip_address
from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.database.models import User, AccountChallenge
from app.database.repositories import SQLRepository
from app.services import accounts


class IdentityRequest(BaseModel):
    identity: str = Field(min_length=1,max_length=2048)


class EmailRequest(BaseModel):
    email: str = Field(min_length=5,max_length=254)


class CodeRequest(BaseModel):
    token: str = Field(min_length=20,max_length=100)
    code: str = Field(pattern=r'^\d{6}$')
    referrer: str = Field(default='',max_length=32)


class UsernameRequest(BaseModel):
    username: str | None = Field(default=None,max_length=32)


def account_router(database,settings,current_user,session_token,cookie):
    class AccountRoute(APIRoute):
        def get_route_handler(self):
            handler=super().get_route_handler()
            async def checked(request):
                origin=request.headers.get('origin')
                if request.method!='GET' and origin and origin!=settings.web_base_url:
                    raise HTTPException(403,'Запрос должен отправляться с сайта Freeman ONE.')
                try: result=await handler(request)
                except ValueError as error: raise HTTPException(422,str(error))
                result.headers['Cache-Control']='no-store'
                return result
            return checked
    router=APIRouter(route_class=AccountRoute)

    def response(user):
        result=JSONResponse({'ok':True,'freeman_id':user.freeman_id})
        result.set_cookie(cookie,session_token(user.id,user.auth_version),max_age=86400*30,httponly=True,secure=True,samesite='lax',path='/')
        result.headers['Cache-Control']='no-store'
        return result

    async def run(action):
        # Isolate uniqueness conflicts so the outer transaction remains usable.
        error=None
        result=None
        async with database.sessions.begin() as session:
            try:
                async with session.begin_nested():
                    result=await action(session)
            except IntegrityError:
                error=HTTPException(409,'Этот идентификатор уже связан с другим аккаунтом.')
            except (ValueError,RuntimeError) as problem:
                error=HTTPException(422 if isinstance(problem,ValueError) else 503,str(problem))
        if error: raise error
        return result

    async def rate(request,identity,scope='login'):
        host=request.client.host if request.client else 'unknown'
        # Only our local reverse proxy can supply this overwritten header.
        try:
            if ip_address(host).is_private and request.headers.get('x-freeman-client-ip'):
                host=str(ip_address(request.headers['x-freeman-client-ip']))
        except ValueError: pass
        async with database.sessions.begin() as session:
            try:
                await accounts.throttle(session,scope+'-ip',host,30)
                await accounts.throttle(session,scope+'-identity',identity.lower(),10)
            except ValueError as problem:
                error=str(problem)
            else: error=None
        if error: raise HTTPException(429,error)

    class TelegramStatus(BaseModel):
        token: str = Field(min_length=20,max_length=64)

    @router.post('/api/web/auth/telegram/request')
    async def telegram_request(request:Request):
        import secrets
        from app.services import telegram_auth
        secret=secrets.token_urlsafe(32)
        await rate(request,secret,'telegram-request')
        async with database.sessions.begin() as session:
            referrer_name=request.cookies.get('freeman_referrer','')
            inviter=await accounts.find_freeman_id(session,referrer_name) if referrer_name else None
            token=await telegram_auth.initiate(session,secret,inviter.id if inviter and inviter.status=='active' else None)
        result=JSONResponse({'token':token,'url':f'https://t.me/{settings.bot_username}?start=auth_{token}','expires_in':telegram_auth.TTL})
        result.set_cookie(telegram_auth.COOKIE,secret,max_age=telegram_auth.TTL,secure=True,httponly=True,samesite='lax',path='/')
        return result

    @router.post('/api/web/auth/telegram/status')
    async def telegram_status(payload:TelegramStatus,request:Request):
        from app.services import telegram_auth
        secret=request.cookies.get(telegram_auth.COOKIE,'')
        if not secret: raise HTTPException(403,'Вход нужно начать в этом браузере.')
        async with database.sessions.begin() as session:
            user=await telegram_auth.consume(session,payload.token,secret)
        if user is None: return {'status':'pending'}
        result=response(user)
        result.delete_cookie(telegram_auth.COOKIE,path='/')
        return result

    @router.post('/api/web/auth/otp/request')
    async def otp_request(payload:IdentityRequest,request:Request):
        identity=__import__('unicodedata').normalize('NFKC',payload.identity).strip()
        email=accounts.normalize_email(identity) if '@' in identity and not identity.startswith(('https://','http://')) else None
        await rate(request,identity,'otp-request')
        async def action(session):
            if email:
                user=await session.scalar(select(User).where(User.email==email))
                # Identical request and delivery for new and returning addresses.
                token=await accounts.issue_email(session,settings,email,'login_email',user.id if user else None)
                channel='email'; destination=mask_email(email)
            else:
                from app.database.models import VlessCredential
                # A subscription URL identifies its owner; it never authenticates them.
                user=await accounts.resolve_identity(session,identity)
                if not user:
                    credential=await session.scalar(select(VlessCredential).where(
                        VlessCredential.subscription_url==payload.identity.strip(),
                        VlessCredential.status!='deleted'))
                    user=await session.get(User,credential.user_id) if credential else None
                if not user or user.status!='active':
                    raise ValueError('Не удалось продолжить вход. Проверьте данные или используйте Telegram.')
                if user.email and user.email_verified:
                    token=await accounts.issue_email(session,settings,user.email,'login_email',user.id)
                    channel='email'; destination=mask_email(user.email)
                else:
                    return {'step':'password'}
            return {'token':token,'channel':channel,'destination':destination,'resend_after':60,'expires_in':600}
        return await run(action)

    def mask_email(email):
        local,domain=email.split('@',1)
        return local[:1]+'***@'+domain

    @router.post('/api/web/auth/otp/verify')
    async def otp_verify(payload:CodeRequest,request:Request):
        await rate(request,payload.token,'otp-verify')
        error=None; user=None
        async with database.sessions.begin() as session:
            row=await session.scalar(select(AccountChallenge).where(AccountChallenge.token_hash==sha256(payload.token.encode()).hexdigest(),
                AccountChallenge.purpose.in_(['login_email','login_telegram'])))
            if not row: error='Неверный код'
            else:
                try:
                    await accounts.verify_email_code(session,settings,payload.token,payload.code,row.purpose,row.user_id)
                except ValueError as problem: error=str(problem)
            if error is None:
                try:
                    async with session.begin_nested():
                        user=await session.get(User,row.user_id) if row.user_id else await session.scalar(select(User).where(User.email==row.email))
                        if user:
                            if user.status!='active': raise ValueError('Аккаунт недоступен.')
                            if row.purpose=='login_email':
                                if user.email!=row.email: raise ValueError('Способ входа изменился. Запросите новый код.')
                                user.email_verified=True
                            elif not user.telegram_id: raise ValueError('Telegram больше не привязан. Запросите новый код.')
                        elif row.purpose=='login_email':
                            referrer_name=request.cookies.get('freeman_referrer') or payload.referrer
                            inviter=await session.scalar(select(User).where(User.freeman_id.in_(accounts.freeman_id_aliases(referrer_name)),User.status=='active')) if referrer_name else None
                            user=await accounts.create_website_account(SQLRepository(session),row.email,referrer_id=inviter.id if inviter else None)
                        else: raise ValueError('Аккаунт недоступен.')
                        await session.flush()
                except (IntegrityError,ValueError) as problem:
                    error=str(problem) if isinstance(problem,ValueError) else 'Аккаунт изменился. Запросите новый код.'
        if error: raise HTTPException(422,error)
        return response(user)

    @router.post('/api/web/profile/email/request')
    async def link_email_request(payload:EmailRequest,request:Request,freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        email=accounts.normalize_email(payload.email)
        await rate(request,email,'link-email')
        async def action(session):
            owner=await session.scalar(select(User).where(User.email==email))
            if owner and owner.id!=user.id: raise ValueError('Email уже связан с другим аккаунтом.')
            return {'token':await accounts.issue_email(session,settings,email,'link_email',user.id)}
        return await run(action)

    @router.post('/api/web/profile/email/verify')
    async def link_email_verify(payload:CodeRequest,request:Request,freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        await rate(request,payload.token,'verify')
        error=None
        async with database.sessions.begin() as session:
            try: email=await accounts.verify_email_code(session,settings,payload.token,payload.code,'link_email',user.id)
            except ValueError as problem: error=str(problem)
            if error is None:
                try:
                    async with session.begin_nested():
                        saved=await session.get(User,user.id)
                        saved.email=email; saved.email_verified=True
                        await session.flush()
                except IntegrityError: error='Email уже связан с другим аккаунтом.'
        if error: raise HTTPException(422,error)
        return {'email':email,'email_verified':True}

    @router.post('/api/web/profile/username')
    async def username(payload:UsernameRequest,freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        normalized=accounts.normalize_username(payload.username) if payload.username else None
        async def action(session):
            saved=await session.get(User,user.id)
            saved.account_username=normalized
            await session.flush()
            return {'username':normalized}
        return await run(action)

    @router.post('/api/web/profile/sessions/revoke')
    async def revoke_sessions(freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        from sqlalchemy import update
        async with database.sessions.begin() as session:
            await session.execute(update(User).where(User.id==user.id).values(auth_version=User.auth_version+1))
        result=JSONResponse({'ok':True})
        result.delete_cookie(cookie,path='/',secure=True,httponly=True,samesite='lax')
        return result

    class UnlinkTelegram(BaseModel):
        password: str = Field(min_length=1, max_length=128)

    @router.post('/api/web/profile/telegram/unlink')
    async def telegram_unlink(payload:UnlinkTelegram,request:Request,freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        await rate(request,str(user.id),'telegram-unlink')
        from sqlalchemy import update
        from app.database.models import Delivery,ChatNotice,CustomerContact
        from app.web.guest_purchase import password_matches
        import asyncio
        async with database.sessions.begin() as session:
            saved=await session.scalar(select(User).where(User.id==user.id).with_for_update())
            if settings.is_admin(saved.telegram_id):raise HTTPException(403,'Привязка служебного аккаунта меняется через владельца.')
            if not saved.email or not saved.email_verified or not saved.password_hash:
                raise HTTPException(409,'Сначала подтвердите почту и установите пароль, чтобы сохранить доступ к аккаунту.')
            if not await asyncio.to_thread(password_matches,payload.password,saved.password_hash):raise HTTPException(401,'Текущий пароль неверен.')
            old=saved.telegram_id
            if not old:raise HTTPException(409,'Telegram уже отвязан.')
            saved.telegram_id=None;saved.username=None;saved.auth_version+=1
            await session.execute(update(CustomerContact).where(CustomerContact.user_id==saved.id,CustomerContact.channel=='telegram',CustomerContact.destination==str(old)).values(enabled=False))
            await session.execute(update(AccountChallenge).where(AccountChallenge.user_id==saved.id,AccountChallenge.consumed.is_(False)).values(consumed=True))
            await session.execute(update(Delivery).where(Delivery.user_id==saved.id,Delivery.status=='pending').values(status='skipped'))
            await session.execute(update(ChatNotice).where(ChatNotice.target_id==old,ChatNotice.kind=='client',ChatNotice.status=='pending').values(status='cancelled'))
            # All business rows keep the same User.id. No transfer or new account.
            await session.flush()
            return response(saved)

    @router.post('/api/web/profile/telegram/link')
    async def telegram_link(freeman_session:str|None=Cookie(default=None)):
        user=await current_user(freeman_session)
        if user.telegram_id: raise HTTPException(409,'Telegram уже связан с аккаунтом.')
        async with database.sessions.begin() as session:
            token=await accounts.issue_telegram_link(session,user.id)
        return {'url':f'https://t.me/{settings.bot_username}?start=link_{token}'}

    @router.get('/r/{referrer}')
    async def referral_link(referrer:str):
        async with database.sessions() as session:
            user=await accounts.find_freeman_id(session,referrer)
            if user and user.status!='active':user=None
        result=RedirectResponse('/buy?referral=1' if user else '/',303)
        if user: result.set_cookie('freeman_referrer',accounts.display_freeman_id(user.freeman_id),max_age=86400*30,secure=True,httponly=True,samesite='lax')
        return result

    return router
