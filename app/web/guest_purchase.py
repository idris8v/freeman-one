"""Public checkout, sharing the bot's tariffs, payment verification and provisioning."""
import asyncio
import hashlib
import hmac
import json
import secrets
from datetime import timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.routing import APIRoute
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field
from sqlalchemy import select, update, or_
from app.database.models import GuestOrder, User, Tariff, Payment, VlessCredential, RemnawaveServer, Subscription, GuestPurchaseContext, now
from app.database.repositories import SQLRepository
from app.services import accounts
from app.services.topups import SubscriptionCheckoutService, YooKassa
from app.services.pricing import quote
from app.services import customer_identity as identity
from app.web.customer_identity import authenticated_customer, resolve_customer


class Draft(BaseModel):
    antig: bool = False
    promo: str = Field(default='',max_length=64)
    months: int | None = Field(default=None, ge=0, le=36)
    tariff_id: int | None = Field(default=None, ge=1)
    key: str = Field(default='', max_length=2048)
    without_email: bool = False
    nonce: str = Field(default='',max_length=100)


class Email(BaseModel):
    email: str = Field(max_length=254)


class Code(BaseModel):
    token: str = Field(min_length=20, max_length=100)
    code: str = Field(pattern=r'^\d{6}$')


class Login(BaseModel):
    identity: str = Field(min_length=1, max_length=2048)
    password: str = Field(min_length=1, max_length=128)

class ChangePassword(BaseModel):
    current: str = Field(min_length=1,max_length=128)
    password: str = Field(min_length=10,max_length=128)


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f'scrypt${salt}${digest}'


def password_matches(password, stored):
    try:
        kind, salt, digest = (stored or '').split('$')
        return kind == 'scrypt' and hmac.compare_digest(password_hash(password, salt).split('$')[2], digest)
    except (ValueError, TypeError):
        return False


def new_password():
    # A pronounceable word plus four digits, with 64**4 * 10000 possibilities.
    syllables = [c+v for c in 'bcdfghjkmnprstvz' for v in 'aeou']
    return ''.join(secrets.choice(syllables) for _ in range(4)).capitalize() + f'{secrets.randbelow(10000):04d}'


async def available(repo, settings):
    if not settings.yookassa_shop_id or not settings.yookassa_secret_key or settings.yookassa_test_mode:
        return False
    if await repo.setting('payment_provider:yookassa', '1') != '1':
        return False
    panel = await repo.session.scalar(select(RemnawaveServer.id).where(
        RemnawaveServer.status == 'active', RemnawaveServer.health == 'online',
        RemnawaveServer.internal_squad_uuid != '', RemnawaveServer.users_total < RemnawaveServer.max_users))
    return bool(panel)


def guest_router(database, settings, session_token, cookie, current_user=None):
    class GuestRoute(APIRoute):
        def get_route_handler(self):
            handler=super().get_route_handler()
            async def checked(request):
                origin=request.headers.get('origin')
                if request.method!='GET' and origin and origin!=settings.web_base_url and not (request.url.hostname in ('127.0.0.1','localhost') and origin==str(request.base_url).rstrip('/')):
                    raise HTTPException(403,'Запрос должен отправляться с сайта Freeman ONE.')
                try:
                    result=await handler(request)
                except IntegrityError:
                    raise HTTPException(409,'Данные уже связаны с аккаунтом. Войдите в личный кабинет.')
                except ValueError as e:
                    raise HTTPException(422,str(e))
                except RuntimeError as e:
                    raise HTTPException(503,str(e))
                result.headers['Cache-Control']='no-store'
                result.headers['Referrer-Policy']='no-referrer'
                return result
            return checked
    router = APIRouter(route_class=GuestRoute)
    cipher = lambda: Fernet(settings.encryption_key.encode())

    async def rate(request, scope):
        # The same trusted proxy header as the existing account router.
        from ipaddress import ip_address
        host = request.client.host if request.client else 'unknown'
        try:
            if ip_address(host).is_private and request.headers.get('x-freeman-client-ip'):
                host = str(ip_address(request.headers['x-freeman-client-ip']))
        except ValueError:
            pass
        async with database.sessions.begin() as s:
            await accounts.throttle(s, 'guest-' + scope, host, 20, 900)

    def has_order_cookie(row, request):
        token = request.cookies.get(f'fo_order_{row.id}', '')
        return bool(token) and hmac.compare_digest(row.token_hash, hashlib.sha256(token.encode()).hexdigest())

    async def order(session, request, oid, lock=False, allow_account=False):
        token = request.cookies.get(f'fo_order_{oid}', '')
        customer = await authenticated_customer(session, request, settings) if allow_account else None
        proof = GuestOrder.token_hash == hashlib.sha256(token.encode()).hexdigest()
        if customer:
            proof = or_(proof, GuestOrder.user_id == customer.id)
        query = select(GuestOrder).where(GuestOrder.id == oid, proof)
        if lock:
            query = query.with_for_update()
        row = await session.scalar(query)
        cookie_access = row is not None and has_order_cookie(row, request)
        if not row or (not cookie_access and (not customer or customer.id != row.user_id)) or row.expires_at.replace(tzinfo=timezone.utc) <= now():
            raise HTTPException(403, 'Ссылка на заказ истекла или открыта в другом браузере. Войдите в аккаунт покупки.')
        if not row.renewal_sid and row.user_id:
            user = await session.get(User, row.user_id)
            version = int(await SQLRepository(session).setting(f'guest_auth_version:{row.id}', '0'))
            if not user or user.status != 'active' or user.auth_version != version:
                raise HTTPException(403, 'Доступ к заказу отменён. Войдите в личный кабинет.')
        return row

    @router.get('/buy')
    @router.get('/buy/renew')
    @router.get('/buy/security')
    async def page():
        return FileResponse(Path(__file__).with_name('static') / 'guest-purchase.html', headers={'Cache-Control': 'no-cache'})

    @router.get('/connect-guide')
    async def connection_guide():
        return FileResponse(Path(__file__).with_name('static') / 'connect-guide.html',
                            headers={'Cache-Control': 'no-cache', 'Referrer-Policy': 'no-referrer'})

    @router.get('/api/buy/{oid}/connection-key')
    async def connection_key(oid: int, request: Request):
        async with database.sessions() as session:
            row = await order(session, request, oid, allow_account=True)
            payment = await session.get(Payment, row.payment_id) if row.payment_id else None
            if not payment or payment.user_id != row.user_id or payment.status != 'order_paid' or not payment.subscription_id:
                raise HTTPException(404, 'Подписка пока недоступна.')
            key = await session.scalar(select(VlessCredential.subscription_url).where(
                VlessCredential.subscription_id == payment.subscription_id,
                VlessCredential.user_id == row.user_id,
                VlessCredential.status == 'active',
                VlessCredential.expires_at > now(),
            ).limit(1))
            if not key:
                raise HTTPException(404, 'Подписка пока недоступна.')
            return JSONResponse({'key': key}, headers={'Cache-Control': 'no-store'})

    @router.get('/buy/login')
    async def shared_login():
        from fastapi.responses import RedirectResponse
        return RedirectResponse('/?return=/buy/security',status_code=303)

    @router.get('/api/buy/catalog')
    async def catalog(request: Request):
        from app.services.catalog import public_price
        async with database.sessions() as s:
            repo = SQLRepository(s)
            tariffs = []
            for t in await s.scalars(select(Tariff).order_by(Tariff.months)):
                if await repo.tariff_setting(t, 'tariff_enabled', '1') == '1':
                    tariffs.append(dict(id=t.id,months=t.months, base_price=t.price, amount=await public_price(repo, 'vpn', t.id, t.price, False), devices=t.device_limit))
            from app.web.purchase_antig import options
            antig=await options(repo,[await s.get(Tariff,t["id"]) for t in tariffs])
            from app.services.promo_visibility import enabled
            referrer = request.cookies.get('freeman_referrer','')
            inviter = await accounts.find_freeman_id(s, referrer) if referrer and await repo.setting('referral_enabled','1') == '1' else None
            bot_start = f'?start=site_ref_{inviter.id}' if inviter and inviter.status == 'active' else ''
            return JSONResponse(dict(promos_enabled=await enabled(repo),tariffs=tariffs,antig=antig, available=await available(repo, settings), bot_url='https://t.me/' + settings.bot_username + bot_start), headers={'Cache-Control': 'no-store'})

    @router.get('/api/buy/subscriptions')
    async def own_subscriptions(request: Request):
        if not current_user:
            raise HTTPException(401, 'Войдите в личный кабинет.')
        authenticated = await current_user(request.cookies.get(cookie))
        if not authenticated:
            raise HTTPException(401, 'Войдите в личный кабинет.')
        async with database.sessions() as s:
            repo = SQLRepository(s)
            from app.services.billing import BillingService
            result = []
            for c in await s.scalars(select(VlessCredential).where(VlessCredential.user_id == authenticated.id, VlessCredential.status != 'deleted')):
                try:
                    sub, key = await BillingService(repo, settings).valid(authenticated, c.subscription_id)
                except ValueError:
                    continue
                result.append(dict(name=key.name, key=c.subscription_url, devices=sub.device_limit))
            return dict(subscriptions=result)

    @router.post('/api/buy/lookup')
    async def lookup(payload: Draft, request: Request):
        await rate(request, 'lookup')
        async with database.sessions() as s:
            repo = SQLRepository(s)
            c = await s.scalar(select(VlessCredential).where(VlessCredential.subscription_url == payload.key.strip(), VlessCredential.status != 'deleted'))
            if not c:
                raise HTTPException(422, 'Ключ не найден. Вставьте ссылку подключения целиком.')
            user = await s.get(User, c.user_id)
            from app.services.billing import BillingService
            sub, key = await BillingService(repo, settings).valid(user, c.subscription_id)
            tariffs = []
            for t in await s.scalars(select(Tariff).order_by(Tariff.months)):
                if t.device_limit == sub.device_limit and await repo.tariff_setting(t, 'tariff_enabled', '1') == '1':
                    tariffs.append(dict(id=t.id,months=t.months, base_price=t.price, amount=(await quote(repo, user.id, t, sub.id))[0], devices=t.device_limit))
            return dict(name=key.name, devices=sub.device_limit, expires_at=sub.expires_at.isoformat(), status='active' if sub.expires_at.replace(tzinfo=timezone.utc)>now() else 'expired', tariffs=tariffs)

    @router.post('/api/buy/draft')
    async def draft(payload: Draft, request: Request):
        await rate(request, 'draft')
        result=JSONResponse({})
        async with database.sessions.begin() as s:
            repo=SQLRepository(s)
            if len(payload.nonce)<32:raise HTTPException(422,'Обновите страницу покупки.')
            await identity.lock_nonce(s,payload.nonce)
            ctx=await identity.context(s,request,result)
            resolved,source=await resolve_customer(s,request,settings)
            request_hash=hashlib.sha256(payload.nonce.encode()).hexdigest()
            token=hmac.new(settings.encryption_key.encode(),('guest:'+payload.nonce).encode(),hashlib.sha256).hexdigest()
            previous=await s.scalar(select(GuestOrder).where(GuestOrder.request_hash==request_hash))
            tariff=await repo.lock_tariff(await repo.select_tariff(payload.months,payload.tariff_id))
            if not tariff or await repo.tariff_setting(tariff, 'tariff_enabled','1')!='1':raise HTTPException(422,'Тариф недоступен.')
            if payload.antig and payload.key:raise HTTPException(422,'Антиглушилка выбирается при покупке новой подписки.')
            if payload.antig and not previous:
                from app.services.bot_antig_price import require_enabled
                await require_enabled(repo)
            if previous:
                if (await repo.setting(f'guest_antig:{previous.id}','0')=='1')!=payload.antig:raise HTTPException(409,'Выбор антиглушилки изменился. Создайте новый заказ.')
                previous_tid=await repo.setting(f'guest_tariff:{previous.id}')
                if not previous_tid:
                    legacy=await repo.tariff(previous.months)
                    previous_tid=str(legacy.id) if legacy else ''
                if previous.checkout_id!=ctx.id or previous.months!=tariff.months or previous_tid!=str(tariff.id) or previous.expires_at.replace(tzinfo=timezone.utc)<=now():raise HTTPException(409,'Откройте новый заказ в этом браузере.')
                if resolved and previous.user_id and previous.user_id!=resolved.id:raise HTTPException(409,'Аккаунт изменился. Создайте новый заказ.')
                row=previous
            else:
                if not await available(repo,settings):raise HTTPException(503,'Покупка временно недоступна.')
                sid=0;user=resolved
                if payload.key:
                    credential=await s.scalar(select(VlessCredential).where(VlessCredential.subscription_url==payload.key.strip(),VlessCredential.status!='deleted'))
                    if not credential:raise HTTPException(422,'Ключ не найден. Проверьте ссылку подписки.')
                    if resolved and credential.user_id!=resolved.id:raise HTTPException(409,'Этот ключ принадлежит другому аккаунту. Выберите «Это не мой аккаунт» или войдите в его аккаунт.')
                    user=await s.get(User,credential.user_id);sid=credential.subscription_id
                    from app.services.billing import BillingService
                    sub,_=await BillingService(repo,settings).valid(user,sid)
                    if tariff.device_limit!=sub.device_limit:raise HTTPException(422,'Для продления выберите прежнее количество устройств.')
                row=await repo.add(GuestOrder(user_id=user.id if user else None,checkout_id=ctx.id,request_hash=request_hash,token_hash=hashlib.sha256(token.encode()).hexdigest(),months=tariff.months,renewal_sid=sid,secret='',expires_at=now()+timedelta(days=7)))
                await repo.set_setting(f'guest_tariff:{row.id}',str(tariff.id))
                await repo.set_setting(f'guest_antig:{row.id}','1' if payload.antig else '0')
                referrer = request.cookies.get('freeman_referrer', '')
                inviter = await accounts.find_freeman_id(s, referrer) if referrer and await repo.setting('referral_enabled','1') == '1' else None
                if not user and inviter and inviter.status == 'active':
                    await repo.set_setting(f'guest_referrer:{row.id}', str(inviter.id))
                if user:await repo.set_setting(f'guest_auth_version:{row.id}',str(user.auth_version))
            from app.services.freeman_ai.attribution import bind_order
            await bind_order(s,settings,request,row)
            from app.services.catalog import public_price
            amount=(await quote(repo,row.user_id,tariff,row.renewal_sid))[0] if row.user_id else await public_price(repo,'vpn',tariff.id,tariff.price,False)
            code=payload.promo.strip().upper()
            if code and not row.payment_id:
                from app.services.catalog import promo_valid
                promo=await repo.promo(code)
                if not promo or promo.effect!='percent_discount' or not await promo_valid(repo,promo):raise HTTPException(422,'Промокод недействителен или отключён.')
                amount=min(amount,tariff.price*(100-promo.value)//100)
                await repo.set_setting(f'guest_promo:{row.id}',code)
            if payload.antig:
                from app.services.bot_antig_price import tariff_total
                amount+=await tariff_total(repo,tariff)
                if not row.payment_id:await repo.set_setting(f'guest_antig_total:{row.id}',str(amount))
            payment=await s.get(Payment,row.payment_id) if row.payment_id else None
            payload_out=dict(order_id=row.id,amount=payment.amount if payment else amount,renewal=bool(row.renewal_sid))
            result.body=json.dumps(payload_out).encode();result.headers['content-length']=str(len(result.body))
            result.set_cookie(f'fo_order_{row.id}',token,max_age=604800,secure=True,httponly=True,samesite='lax',path='/')
            result.headers['Cache-Control']='no-store'
            return result

    async def repo_email_binding(session,oid,token):
        await SQLRepository(session).set_setting(f'guest_email_challenge:{oid}',hashlib.sha256(token.encode()).hexdigest())

    @router.post('/api/buy/{oid}/email')
    async def email(oid: int, payload: Email, request: Request):
        await rate(request, 'email')
        async with database.sessions.begin() as s:
            row = await order(s, request, oid)
            if row.renewal_sid or row.payment_id:
                raise HTTPException(409, 'Почту можно привязать в личном кабинете.')
            address = accounts.normalize_email(payload.email)
            owner=await s.scalar(select(User).where(User.email==address,User.email_verified.is_(True)))
            if owner and row.user_id and row.user_id!=owner.id:raise HTTPException(409,'Почта другого аккаунта. Войдите в нужный аккаунт перед покупкой.')
            token = await accounts.issue_email(s, settings, address, 'guest_email', row.user_id)
            await repo_email_binding(s, row.id, token)
            return JSONResponse(dict(token=token), headers={'Cache-Control': 'no-store'})

    @router.post('/api/buy/{oid}/email/verify')
    async def verify(oid: int, payload: Code, request: Request):
        await rate(request, 'verify')
        problem = None
        async with database.sessions.begin() as s:
            row = await order(s, request, oid, True)
            if row.renewal_sid or row.payment_id:
                raise HTTPException(409, 'Заказ уже оформлен.')
            if await SQLRepository(s).setting(f'guest_email_challenge:{row.id}')!=hashlib.sha256(payload.token.encode()).hexdigest():raise HTTPException(422,'Запросите код для этого заказа.')
            try:
                address = await accounts.verify_email_code(s, settings, payload.token, payload.code, 'guest_email', row.user_id)
            except ValueError as e:
                problem = str(e)
            if not problem:
                row.verified_email=address
                existing=await s.scalar(select(User).where(User.email==address))
                if existing:
                    if row.user_id and row.user_id!=existing.id:raise HTTPException(409,'Почта связана с другим аккаунтом.')
                    row.user_id=existing.id;row.secret=''
                    await SQLRepository(s).set_setting(f'guest_auth_version:{row.id}',str(existing.auth_version))
        if problem:
            raise HTTPException(422, problem)
        result=JSONResponse({'verified':True})
        if existing:
            result.set_cookie(cookie,session_token(existing.id,existing.auth_version),max_age=2592000,secure=True,httponly=True,samesite='lax',path='/')
        return result

    @router.post('/api/buy/{oid}/pay')
    async def pay(oid: int, payload: Draft, request: Request):
        await rate(request, 'pay')
        async with database.sessions.begin() as s:
            row = await order(s, request, oid, True)
            repo = SQLRepository(s)
            if row.months!=payload.months:raise HTTPException(409,'Тариф изменился. Создайте новый заказ.')
            antig=await repo.setting(f'guest_antig:{row.id}','0')=='1'
            if antig and not row.payment_id:
                from app.services.bot_antig_price import require_enabled
                await require_enabled(repo)
            active_customer,active_source=await resolve_customer(s,request,settings)
            if active_customer and row.user_id and row.user_id!=active_customer.id:raise HTTPException(409,'Аккаунт изменился. Создайте новый заказ.')
            if not row.payment_id:
                ctx=await s.get(GuestPurchaseContext,row.checkout_id,with_for_update=True) if row.checkout_id else None
                resolved,source=await resolve_customer(s,request,settings)
                if resolved and row.user_id and row.user_id!=resolved.id:raise HTTPException(409,'Аккаунт изменился. Создайте новый заказ.')
                if not row.user_id and resolved:row.user_id=resolved.id
                if not row.user_id and ctx and ctx.user_id:
                    pending=await s.get(User,ctx.user_id)
                    if pending and pending.status=='active' and pending.auth_version==ctx.auth_version:
                        row.user_id=pending.id;row.secret=ctx.secret
                if not row.user_id:
                    password=new_password()
                    user=await repo.add(User(registration_source='website_guest',first_name='',password_hash=await asyncio.to_thread(password_hash,password)))
                    inviter_id = await repo.setting(f'guest_referrer:{row.id}', '')
                    inviter = await s.get(User, int(inviter_id)) if inviter_id.isdigit() else None
                    if inviter and inviter.status == 'active' and inviter.id != user.id and await repo.setting('referral_enabled','1') == '1':
                        from app.database.models import Referral
                        user.referrer_id = inviter.id
                        await repo.add(Referral(inviter_id=inviter.id, invitee_id=user.id))
                    row.user_id=user.id;row.secret=cipher().encrypt(password.encode()).decode()
                    if ctx:ctx.user_id=user.id;ctx.auth_version=user.auth_version;ctx.secret=row.secret
                user=await s.get(User,row.user_id)
                if user.status!='active':raise HTTPException(403,'Аккаунт недоступен.')
                await repo.set_setting(f'guest_auth_version:{row.id}',str(user.auth_version))
                if row.verified_email:
                    owner=await s.scalar(select(User.id).where(User.email==row.verified_email,User.id!=user.id))
                    if owner:raise HTTPException(409,'Почта связана с другим аккаунтом.')
                    user.email=row.verified_email;user.email_verified=True
                    await accounts.ensure_account(repo,user)
                if not row.renewal_sid and not user.email_verified and not payload.without_email:raise HTTPException(422,'Подтвердите почту или выберите покупку без почты.')
            user=await s.get(User,row.user_id)
            if not row.payment_id:
                if not await available(repo, settings):
                    raise HTTPException(503, 'Выдача подключений временно недоступна.')
                code=await repo.setting(f'guest_promo:{row.id}','')
                if code:
                    from app.services.catalog import promo_valid
                    from app.services.domain import PromoService
                    promo=await repo.promo(code)
                    if not promo or not await promo_valid(repo,promo):raise HTTPException(422,'Промокод отключён. Создайте новый заказ.')
                    await PromoService(repo).apply(user.id,code)
                service = SubscriptionCheckoutService(repo, settings)
                tid=await repo.setting(f'guest_tariff:{row.id}')
                # Commit the local invoice and provider idempotency key BEFORE HTTP.
                # A lost response or rollback must retry this exact payment.
                if antig:
                    from app.web.purchase_antig import prepare, create_order
                    tariff=await repo.tariff(f'id{tid}' if tid else row.months)
                    base=(await quote(repo,user.id,tariff,0))[0]
                    prepared=await prepare(repo,settings,tariff,base,True,int(await repo.setting(f'guest_antig_total:{row.id}','0')))
                    p=await create_order(repo,settings,user,tariff,prepared,method='yookassa',return_url=settings.web_base_url+f'/buy?receipt={oid}')
                else:
                    p = await service.create_order(user, f'id{tid}' if tid else row.months, sid=row.renewal_sid, return_url=settings.web_base_url + f'/buy?receipt={oid}')
                row.payment_id = p.id
                await repo.set_setting(f'guest_payment:{p.id}', str(row.id))
        async with database.sessions.begin() as s:
            row = await order(s, request, oid, True)
            repo = SQLRepository(s)
            payment = await s.get(Payment, row.payment_id, with_for_update=True)
            if not payment or payment.user_id != row.user_id:
                raise HTTPException(409, 'Заказ недоступен. Войдите в личный кабинет.')
            if payment.status not in ('order_paid', 'order_canceled', 'order_failed'):
                await SubscriptionCheckoutService(repo, settings).render(
                    f'order_pay:{payment.id}', await s.get(User, row.user_id))
            url = await repo.setting(f'order_url:{payment.id}')
            return JSONResponse(dict(url=url, status=payment.status), headers={'Cache-Control': 'no-store'})

    @router.get('/api/buy/{oid}/receipt')
    async def receipt(oid: int, request: Request):
        async with database.sessions.begin() as s:
            row = await order(s, request, oid, True, allow_account=True)
            p = await s.get(Payment, row.payment_id) if row.payment_id else None
            if not p:
                return JSONResponse({'status': 'draft'}, headers={'Cache-Control': 'no-store'})
            repo = SQLRepository(s)
            if p.status == 'order_pending':
                provider_data = await YooKassa(settings).get(p.provider_id)
                service = SubscriptionCheckoutService(repo, settings)
                # A failed provision must roll back its claim, keeping webhook retries possible.
                try:
                    async with s.begin_nested():
                        await service.credit(p, provider_data)
                except (ValueError, RuntimeError, OSError):
                    await s.refresh(p)
                    from app.web.post_payment import subscription_confirmed
                    verified = await subscription_confirmed(p, provider_data, service, settings)
                    if not verified:
                        raise
                    return JSONResponse({'status': 'order_processing', 'confirmed': True}, headers={'Cache-Control': 'no-store'})
            from app.services.email_delivery import prepare_mail
            await prepare_mail(repo)
            result = dict(status=p.status, months=row.months, created_at=p.created_at.replace(tzinfo=timezone.utc).isoformat())
            # Recover redirects even when the original /pay response was lost.
            if p.provider_id and p.status == 'order_pending':
                result['payment_url'] = await repo.setting(f'order_url:{p.id}')
            if p.status == 'order_paid':
                c = await s.scalar(select(VlessCredential).where(VlessCredential.subscription_id == p.subscription_id, VlessCredential.user_id == row.user_id))
                if not c or not c.subscription_url:
                    return JSONResponse({'status': 'order_processing', 'confirmed': True}, headers={'Cache-Control': 'no-store'})
                user = await s.get(User, row.user_id)
                subscription = await s.get(Subscription, p.subscription_id)
                result['devices'] = subscription.device_limit if subscription else 1
                result.update(key=c.subscription_url, expires_at=c.expires_at.isoformat(), renewal=bool(row.renewal_sid), freeman_id=accounts.display_freeman_id(user.freeman_id))
                if not row.renewal_sid and row.secret and has_order_cookie(row, request):
                    if not await repo.setting(f'guest_first_receipt:{row.id}'):
                        await repo.set_setting(f'guest_first_receipt:{row.id}',str(int(now().timestamp())))
                    result['password'] = cipher().decrypt(row.secret.encode()).decode() if row.secret else ''
                    result['email'] = user.email if user.email_verified else ''
            result['authenticated']=False
            response=JSONResponse(result, headers={'Cache-Control': 'no-store'})
            if p.status=='order_paid':
                authenticated=await authenticated_customer(s,request,settings)
                resolved,source=await resolve_customer(s,request,settings)
                user=await s.get(User,row.user_id)
                if row.secret and has_order_cookie(row, request) and not await repo.setting(f'guest_auto_session:{row.id}') and (not authenticated or authenticated.id==user.id):
                    response.set_cookie(cookie,session_token(user.id,user.auth_version),max_age=2592000,secure=True,httponly=True,samesite='lax',path='/')
                    await repo.set_setting(f'guest_auto_session:{row.id}','1')
                    result['authenticated']=True
                    response.body=json.dumps(result).encode();response.headers['content-length']=str(len(response.body))
                    await identity.attach_trust(s,request,response,user)
                elif resolved and resolved.id==user.id:await identity.attach_trust(s,request,response,user)
                if authenticated and authenticated.id==user.id:result['authenticated']=True
                response.body=json.dumps(result).encode();response.headers['content-length']=str(len(response.body))
            return response

    @router.get('/api/buy/{oid}/qr')
    async def receipt_qr(oid: int, request: Request):
        from io import BytesIO
        import qrcode
        async with database.sessions() as s:
            row = await order(s, request, oid, allow_account=True)
            payment = await s.get(Payment, row.payment_id) if row.payment_id else None
            if not payment or payment.status != 'order_paid':
                raise HTTPException(409, 'Подписка ещё готовится.')
            credential = await s.scalar(select(VlessCredential).where(VlessCredential.subscription_id == payment.subscription_id, VlessCredential.user_id == row.user_id))
            if not credential or not credential.subscription_url:
                raise HTTPException(409, 'Подписка ещё готовится.')
            buffer = BytesIO()
            qrcode.make(credential.subscription_url).save(buffer, format='PNG')
            return Response(buffer.getvalue(), media_type='image/png', headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

    @router.post('/api/buy/{oid}/enter')
    async def enter(oid: int, request: Request):
        async with database.sessions.begin() as s:
            row = await order(s, request, oid, True)
            p = await s.get(Payment, row.payment_id) if row.payment_id else None
            if row.renewal_sid or not p or p.status != 'order_paid':
                raise HTTPException(403, 'Войдите по логину и паролю.')
            repo = SQLRepository(s)
            user = await s.get(User, row.user_id)
            if user.status!='active' or int(await repo.setting(f'guest_auth_version:{row.id}','0'))!=user.auth_version or not row.secret:
                raise HTTPException(403,'Быстрый вход отменён. Введите логин и пароль.')
            first=int(await repo.setting(f'guest_first_receipt:{row.id}','0'))
            if not first or now().timestamp()-first>600:
                raise HTTPException(403,'Быстрый вход действует 10 минут. Введите логин и пароль.')
            if await repo.setting(f'guest_enter:{row.id}') == '1':
                raise HTTPException(409, 'Быстрый вход уже использован. Введите логин и пароль.')
            await repo.set_setting(f'guest_enter:{row.id}', '1')
            user = await s.get(User, row.user_id)
            result = JSONResponse({'ok': True})
            result.set_cookie(cookie, session_token(user.id, user.auth_version), max_age=2592000, secure=request.url.scheme == 'https', httponly=True, samesite='lax', path='/')
            return result

    @router.post('/api/buy/login')
    async def login(payload: Login, request: Request):
        await rate(request, 'login')
        async with database.sessions.begin() as s:
            await accounts.throttle(s, 'guest-password', payload.identity.strip().lower(), 8, 900)
        async with database.sessions() as s:
            user = await accounts.resolve_identity(s, payload.identity)
            if not user:
                c = await s.scalar(select(VlessCredential).where(VlessCredential.subscription_url == payload.identity.strip(), VlessCredential.status != 'deleted'))
                user = await s.get(User, c.user_id) if c else None
            stored = user.password_hash if user else password_hash('dummy-password', '00000000000000000000000000000000')
            good = await asyncio.to_thread(password_matches, payload.password, stored)
            if not user or user.status != 'active' or not good:
                raise HTTPException(401, 'Неверный логин или пароль.')
            result = JSONResponse({'ok': True})
            result.set_cookie(cookie, session_token(user.id, user.auth_version), max_age=2592000, secure=request.url.scheme == 'https', httponly=True, samesite='lax', path='/')
            result.headers['Cache-Control'] = 'no-store'
            return result

    @router.post('/api/buy/password')
    async def change_password(payload:ChangePassword,request:Request):
        await rate(request,'password-change')
        authenticated=await current_user(request.cookies.get(cookie))
        async with database.sessions.begin() as s:
            user=await s.scalar(select(User).where(User.id==authenticated.id).with_for_update())
            if not await asyncio.to_thread(password_matches,payload.current,user.password_hash):
                raise HTTPException(401,'Текущий пароль неверен.')
            user.password_hash=await asyncio.to_thread(password_hash,payload.password)
            user.auth_version+=1
            # Discard the generated password after a customer replaces it.
            await s.execute(update(GuestOrder).where(GuestOrder.user_id==user.id).values(secret=''))
            await s.execute(update(GuestPurchaseContext).where(GuestPurchaseContext.user_id==user.id).values(secret=''))
            result=JSONResponse({'ok':True})
            result.set_cookie(cookie,session_token(user.id,user.auth_version),max_age=2592000,secure=request.url.scheme=='https',httponly=True,samesite='lax',path='/')
            return result

    return router
