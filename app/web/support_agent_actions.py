"""V2.1 actions: fixed HTTP paths, credentials only in POST bodies/cookies.

Read-only, single-key capability is delivered in the URL fragment, which never
reaches an HTTP access log. It cannot authorize any key mutation.
"""
import time
from pathlib import Path
from typing import Literal
from fastapi import APIRouter,Request,HTTPException
from fastapi.responses import Response,FileResponse,JSONResponse
from pydantic import BaseModel,Field
from app.database.models import AIConversation
from app.services.freeman_ai.security import fingerprint
from app.services.freeman_ai.v21.persistence import DurableRepository,PanelAdapter
from app.services.freeman_ai.v21.actions import ActionDenied
from app.services.freeman_ai.v21.interactions import verify_read_capability

class Token(BaseModel):
    token:str=Field(pattern=r'^[A-Za-z0-9_-]{32}$')
class Decision(Token):
    decision:Literal['open','confirm','cancel']='open'

def actions_router(database,settings,current_user):
    router=APIRouter()
    def origin(request):
        if request.headers.get('sec-fetch-site')=='cross-site' or request.headers.get('origin') not in (None,settings.web_base_url.rstrip('/')):
            raise HTTPException(403,'Запрос должен быть отправлен с сайта Freeman ONE.')
    async def access(request,token,*,read_only=False):
        from app.services.freeman_ai.v21.integration import active
        row=await DurableRepository(database.sessions,settings).get(token)
        if not row:raise HTTPException(404,'Действие недоступно.')
        async with database.sessions() as s:
            if not await active(s):raise HTTPException(503,'Помощник временно выключен.')
            c=await s.get(AIConversation,int(row['subject']))
            if not c or c.mode!='ai':raise HTTPException(409,'Диалог сейчас у специалиста или закрыт.')
            user=None
            try:user=await current_user(request.cookies.get('freeman_session'))
            except HTTPException:pass
            ok=bool(user and user.status=='active' and c.user_id==user.id)
            if not ok and c.channel=='website' and not c.user_id:
                browser=request.cookies.get('freeman_ai_browser','')
                ok=bool(browser and c.browser_hash==fingerprint(settings,'browser:'+browser))
            if not ok and c.channel=='support_agent_website':
                try:
                    from cryptography.fernet import Fernet
                    browser=Fernet(settings.encryption_key.encode()).decrypt(request.cookies.get('fo_support_agent','').encode(),ttl=30*86400).decode()
                    ok=c.browser_hash==fingerprint(settings,'support:'+c.channel+':'+browser)
                except Exception:ok=False
            # Delegated capability is read-only and exposes no owner/account data.
            if read_only and row['status']=='command' and row['kind'] in ('copy','import','qr','renew'):
                ok=row['expires']>time.time()
            if not ok:raise HTTPException(404,'Действие недоступно в этом аккаунте.')
            return c.id,row

    @router.get('/support/action')
    @router.get('/support/connect')
    @router.get('/support/renew')
    async def page():
        return FileResponse(Path(__file__).with_name('static')/'agent-action.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})

    @router.post('/api/web/support/agent-read')
    async def inspect(payload:Token,request:Request):
        origin(request)
        from app.services.freeman_ai.v21.registry import incy_link
        cid,row=await access(request,payload.token,read_only=True)
        if row['status']!='command' or row['kind'] not in ('copy','import','qr','renew'):
            return dict(kind=row['kind'],confirmation_required=True)
        async with PanelAdapter(database.sessions,settings,cid).context(row['key'],without_panel=True) as (s,b,c,api):
            try:verify_read_capability(settings,row,c.subscription_url)
            except ActionDenied:raise HTTPException(410,'Запросите новую ссылку в чате.')
            return JSONResponse(dict(kind=row['kind'],key=c.subscription_url,deep_link=incy_link(c.subscription_url) if row['kind']=='import' else None),headers={'Cache-Control':'no-store'})

    @router.post('/api/web/support/agent-action')
    async def execute(payload:Decision,request:Request):
        origin(request);cid,row=await access(request,payload.token)
        from app.services.freeman_ai.v21.interactions import handle
        try:result=await handle(database.sessions,settings,payload.token,cid,payload.decision)
        except (ActionDenied,PermissionError):raise HTTPException(409,'Состояние изменилось. Запросите актуальный список и подтвердите действие заново.')
        except Exception:raise HTTPException(503,'Не удалось подтвердить действие. Не повторяйте перевыпуск — сначала проверим результат.')
        from app.services.freeman_ai import service
        async with database.sessions.begin() as s:
            c=await s.get(AIConversation,cid)
            if c.mode=='ai':
                actions=[dict(type='agent_version',version='2.1')]+result.get('actions',[])
                outcome = result.get('status','done')
                await service.append(s,settings,c,result['text'],'v21-action:'+payload.token+':'+payload.decision+':'+outcome,'assistant',actions,mirror_ticket=True)
        return JSONResponse(result,headers={'Cache-Control':'no-store'})

    @router.post('/api/web/support/agent-qr')
    async def qr(payload:Token,request:Request):
        origin(request);cid,row=await access(request,payload.token,read_only=True)
        if row['status']!='command' or row['kind']!='qr':raise HTTPException(404,'QR недоступен.')
        from app.services.freeman_ai.v21.presentation import verified_qr
        async with PanelAdapter(database.sessions,settings,cid).context(row['key'],without_panel=True) as (s,b,c,api):
            try:verify_read_capability(settings,row,c.subscription_url)
            except ActionDenied:raise HTTPException(410,'Запросите новую ссылку в чате.')
            from anyio import to_thread
            image=await to_thread.run_sync(verified_qr,c.subscription_url)
        return Response(image,media_type='image/png',headers={'Cache-Control':'private, no-store','Referrer-Policy':'no-referrer'})
    return router
