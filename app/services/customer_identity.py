"""First-party recognition. A browser token never authorizes account operations."""
import hashlib, secrets, re, unicodedata, time
from datetime import timedelta,timezone
from cryptography.fernet import Fernet,InvalidToken
from sqlalchemy import select,update,text
from app.database.models import AppSetting,User,TrustedCustomerDevice,GuestPurchaseContext,GuestOrder,Payment,now
from app.services.accounts import display_freeman_id
DEVICE_COOKIE='freeman_device'
CONTEXT_COOKIE='freeman_checkout'
LIFETIME=180*86400

def display_name(customer):
    raw=getattr(customer,'first_name',None) or ''
    name=' '.join(unicodedata.normalize('NFKC',raw).split())
    invalid=name.casefold() in {'user','пользователь','новый пользователь','guest','unknown','anonymous','no name','клиент','покупатель','гость','новый клиент'}
    invalid=invalid or bool(re.fullmatch(r'(?:user|guest|пользователь|client)[ _#-]*\d+',name,re.I))
    invalid=invalid or '@' in name or '://' in name or not any(c.isalpha() for c in name)
    invalid=invalid or bool(re.fullmatch(r'[0-9a-f]{8,}|[0-9a-f-]{32,}',name,re.I))
    invalid=invalid or bool(re.fullmatch(r'(?=.*\d)[A-Za-z0-9_-]{8,}',name))
    invalid=invalid or name.count('Р')+name.count('С')>5 and any(c in name for c in 'ЃЏџµ')
    invalid=invalid or any(unicodedata.category(c).startswith('C') for c in name)
    return display_freeman_id(customer.freeman_id) or f'FO-{customer.id}' if invalid or not name else name[:100]

def digest(token):return hashlib.sha256(token.encode()).hexdigest()
def token(request,name):
    v=request.cookies.get(name,'')
    return v if re.fullmatch(r'[A-Za-z0-9_-]{43}',v) else ''
def set_token(response,name,value,age=LIFETIME):response.set_cookie(name,value,max_age=age,secure=True,httponly=True,samesite='lax',path='/')
def metadata(request):
    ua=request.headers.get('user-agent','')[:512]
    platform='iPhone' if 'iPhone' in ua else 'iPad' if 'iPad' in ua else 'Android' if 'Android' in ua else 'Windows' if 'Windows' in ua else 'Mac' if 'Macintosh' in ua else 'Linux' if 'Linux' in ua else 'Браузер'
    browser='Edge' if 'Edg/' in ua else 'Chrome' if 'Chrome/' in ua or 'CriOS/' in ua else 'Firefox' if 'Firefox/' in ua or 'FxiOS/' in ua else 'Safari' if 'Safari/' in ua else 'Браузер'
    return ua,platform,browser
async def authenticated(session,request,settings):
    v=request.cookies.get('freeman_session')
    if not v:return None
    try:
        import json
        p=json.loads(Fernet(settings.encryption_key.encode()).decrypt(v.encode(),ttl=31*86400))
        if p.get('expires',0)<time.time():return None
        u=await session.get(User,int(p.get('user_id',0)))
        return u if u and u.status=='active' and u.auth_version==p.get('version',0) else None
    except (InvalidToken,ValueError,TypeError):return None
async def enabled(session):
    flag=await session.get(AppSetting,'customer_identity_enabled')
    return not flag or flag.value!='0'
async def trusted(session,request):
    if not await enabled(session):return None
    raw=token(request,DEVICE_COOKIE)
    if not raw:return None
    d=await session.scalar(select(TrustedCustomerDevice).where(TrustedCustomerDevice.token_hash==digest(raw)))
    if not d or not d.is_active or d.expires_at.replace(tzinfo=timezone.utc)<=now():return None
    u=await session.get(User,d.user_id)
    if not u or u.status!='active' or u.auth_version!=d.auth_version:
        d.is_active=False;d.revoked_at=now();return None
    d.last_seen_at=now()
    return u
async def context(session,request,response=None):
    raw=token(request,CONTEXT_COOKIE)
    c=await session.scalar(select(GuestPurchaseContext).where(GuestPurchaseContext.token_hash==digest(raw)).with_for_update()) if raw else None
    if not c or c.expires_at.replace(tzinfo=timezone.utc)<=now():
        if response is None:return None
        raw=secrets.token_urlsafe(32);c=GuestPurchaseContext(token_hash=digest(raw),expires_at=now()+timedelta(days=30));session.add(c);await session.flush();set_token(response,CONTEXT_COOKIE,raw,30*86400)
    return c
async def resolve(session,request,settings):
    u=await authenticated(session,request,settings)
    if u:return u,'authenticated'
    u=await trusted(session,request)
    if u:return u,'trusted'
    if not await enabled(session):return None,'anonymous'
    c=await context(session,request)
    if c and c.user_id and c.auth_version is not None:
        paid=await session.scalar(select(GuestOrder.id).join(Payment,Payment.id==GuestOrder.payment_id).where(GuestOrder.checkout_id==c.id,GuestOrder.user_id==c.user_id,Payment.status=='order_paid').limit(1))
        u=await session.get(User,c.user_id)
        if paid and u and u.status=='active' and u.auth_version==c.auth_version:return u,'purchase'
    return None,'anonymous'
async def attach_trust(session,request,response,user):
    if not await enabled(session):return
    raw=token(request,DEVICE_COOKIE)
    d=await session.scalar(select(TrustedCustomerDevice).where(TrustedCustomerDevice.token_hash==digest(raw)).with_for_update()) if raw else None
    if d and d.is_active and d.user_id==user.id and d.auth_version==user.auth_version and d.expires_at.replace(tzinfo=timezone.utc)>now():d.last_seen_at=now();return
    if d and d.is_active:d.is_active=False;d.revoked_at=now()
    ua,platform,browser=metadata(request);raw=secrets.token_urlsafe(32)
    session.add(TrustedCustomerDevice(user_id=user.id,token_hash=digest(raw),auth_version=user.auth_version,user_agent=ua,platform=platform,browser=browser,expires_at=now()+timedelta(seconds=LIFETIME)))
    set_token(response,DEVICE_COOKIE,raw)
async def forget(session,request,response):
    raw=token(request,DEVICE_COOKIE)
    if raw:await session.execute(update(TrustedCustomerDevice).where(TrustedCustomerDevice.token_hash==digest(raw)).values(is_active=False,revoked_at=now()))
    response.delete_cookie(DEVICE_COOKIE,path='/',secure=True,httponly=True,samesite='lax');response.delete_cookie(CONTEXT_COOKIE,path='/',secure=True,httponly=True,samesite='lax')
async def lock_nonce(session,nonce):
    if session.bind.dialect.name=='postgresql':await session.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':int.from_bytes(hashlib.sha256(nonce.encode()).digest()[:8],'big',signed=True)})
