from fastapi import APIRouter,Request,HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from app.database.models import AppSetting,TrustedCustomerDevice,now
from app.web.cabinet.context import COOKIE,WebContext
from types import SimpleNamespace
from app.services import customer_identity as identity

async def identity_request(session, request):
    """Guest recognition must honor the same logout as the private cabinet."""
    token = request.cookies.get(COOKIE)
    if token and await session.get(AppSetting, WebContext.revocation_key(token)):
        return SimpleNamespace(cookies={k: v for k, v in request.cookies.items() if k != COOKIE})
    return request

async def authenticated_customer(session, request, settings):
    return await identity.authenticated(session, await identity_request(session, request), settings)

async def resolve_customer(session, request, settings):
    return await identity.resolve(session, await identity_request(session, request), settings)

def identity_router(database,settings,current_user):
    router=APIRouter()
    def origin(request):
        if request.headers.get('origin') and request.headers['origin']!=settings.web_base_url or request.headers.get('sec-fetch-site')=='cross-site':raise HTTPException(403,'Запрос должен отправляться с сайта Freeman ONE.')
    @router.get('/api/buy/identity')
    async def summary(request:Request):
        response=JSONResponse({})
        async with database.sessions.begin() as s:
            await identity.context(s,request,response)
            user,source=await resolve_customer(s,request,settings)
            if user and source=='purchase':await identity.attach_trust(s,request,response,user)
            if request.cookies.get(identity.DEVICE_COOKIE) and not await identity.trusted(s,request) and source not in ('authenticated','purchase'):response.delete_cookie(identity.DEVICE_COOKIE,path='/',secure=True,httponly=True,samesite='lax')
            payload={'recognized':bool(user),'authenticated':source=='authenticated','freeman_id':identity.display_freeman_id(user.freeman_id) if user else None,'display_name':user.display_name if user else None}
        response.body=__import__('json').dumps(payload,ensure_ascii=False).encode();response.headers['content-length']=str(len(response.body));response.headers['Cache-Control']='no-store';return response
    @router.post('/api/buy/identity/forget')
    async def forget(request:Request):
        origin(request);response=JSONResponse({'ok':True})
        async with database.sessions.begin() as s:await identity.forget(s,request,response)
        response.delete_cookie('freeman_session',path='/',secure=True,httponly=True,samesite='lax');response.headers['Cache-Control']='no-store';return response
    @router.get('/api/web/trusted-devices')
    async def devices(request:Request):
        user=await current_user(request.cookies.get('freeman_session'))
        async with database.sessions() as s:
            rows=await s.scalars(select(TrustedCustomerDevice).where(TrustedCustomerDevice.user_id==user.id,TrustedCustomerDevice.is_active.is_(True),TrustedCustomerDevice.auth_version==user.auth_version,TrustedCustomerDevice.expires_at>now()).order_by(TrustedCustomerDevice.last_seen_at.desc()))
            current=identity.digest(identity.token(request,identity.DEVICE_COOKIE))
            return JSONResponse({'items':[{'id':r.id,'label':r.platform+' · '+r.browser,'last_seen_at':r.last_seen_at.isoformat(),'created_at':r.created_at.isoformat(),'current':r.token_hash==current} for r in rows]},headers={'Cache-Control':'no-store'})
    @router.delete('/api/web/trusted-devices/{device_id}')
    async def revoke(device_id:int,request:Request):
        origin(request);user=await current_user(request.cookies.get('freeman_session'));response=JSONResponse({'ok':True})
        async with database.sessions.begin() as s:
            d=await s.get(TrustedCustomerDevice,device_id)
            if not d or d.user_id!=user.id:raise HTTPException(404,'Устройство не найдено.')
            d.is_active=False;d.revoked_at=now()
            if d.token_hash==identity.digest(identity.token(request,identity.DEVICE_COOKIE)):
                await identity.forget(s,request,response)
                response.set_cookie('freeman_trust_optout','1',max_age=identity.LIFETIME,secure=True,httponly=True,samesite='lax',path='/')
        response.headers['Cache-Control']='no-store';return response
    return router

class IdentityMiddleware:
    """Observe existing session issuance; never authenticate from recognition alone."""
    def __init__(self,app,database,settings):self.app=app;self.database=database;self.settings=settings
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope.get('path','').startswith('/assets/') or scope.get('path')=='/health':return await self.app(scope,receive,send)
        from starlette.requests import Request
        from starlette.responses import Response
        from http.cookies import SimpleCookie
        from types import SimpleNamespace
        request=Request(scope)
        async def outgoing(message):
            if message['type']=='http.response.start' and message['status']<400:
                cookies=SimpleCookie()
                for k,v in message.get('headers',[]):
                    if k.lower()==b'set-cookie':cookies.load(v.decode('latin1'))
                received=cookies.get('freeman_session')
                # Bind on login and document navigation, never on every API response.
                # Parallel API requests otherwise serialize on the trusted-device row
                # and may each create a device before the first cookie is received.
                if not (received and received.value) and scope.get('path') not in ('/','/buy','/admin/assistant','/admin/ai'):
                    return await send(message)
                auth_request=SimpleNamespace(cookies={'freeman_session':received.value}) if received and received.value else request
                try:
                    async with self.database.sessions.begin() as s:
                        user=await authenticated_customer(s,auth_request,self.settings)
                        if user and (received or request.cookies.get('freeman_trust_optout')!='1'):
                            response=Response()
                            if received:response.delete_cookie('freeman_trust_optout',path='/',secure=True,httponly=True,samesite='lax')
                            device_request=request
                            issued=cookies.get(identity.DEVICE_COOKIE)
                            if issued and issued.value:device_request=SimpleNamespace(cookies={**request.cookies,identity.DEVICE_COOKIE:issued.value},headers=request.headers)
                            await identity.attach_trust(s,device_request,response,user)
                            message['headers'].extend((k,v) for k,v in response.raw_headers if k==b'set-cookie')
                except Exception:
                    # Recognition is optional; never log cookies or response credentials.
                    __import__('logging').getLogger(__name__).warning('Trusted-browser session binding unavailable')
            await send(message)
        await self.app(scope,receive,outgoing)
