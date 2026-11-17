"""Published support guides, shared with Telegram templates; never expose arbitrary replies."""
import json
from pathlib import Path
from types import SimpleNamespace
from fastapi import APIRouter,Cookie,HTTPException
from fastapi.responses import JSONResponse,FileResponse
from sqlalchemy import select
from app.database.models import WebQuickReply,WebSupportAttachment
from app.database.repositories import SQLRepository
from app.web.cabinet.context import SUPPORT_STORAGE
from app.services.freeman_ai.v21.support_content import REGION_GUIDE, setup_media

def guide_attachment(actions, *, standalone=False):
    if isinstance(actions,str):
        try:
            actions=json.loads(actions)
        except ValueError:
            return []
    if not isinstance(actions,list):
        return []
    result=[]
    if any(isinstance(action,dict) and action.get('type')=='guide_video'
           and action.get('material')=='appstore-region' for action in actions):
        base='/api/support-agent' if standalone else '/api/web/support'
        result.append(dict(kind='video',name='Как сменить регион App Store',
                           url=base+'/materials/appstore-region/video'))
    for action in actions:
        if not isinstance(action,dict) or action.get('type')!='setup_image':
            continue
        asset=setup_media(action.get('material'))
        if asset:
            result.append(dict(kind='guide',name='Схема подключения',url='/assets/'+asset))
            break
    return result


REGION_MEDIA_SETTING = "support_appstore_region_media"


async def published_guide(session):
    repo=SQLRepository(session)
    standalone=await repo.setting(REGION_MEDIA_SETTING)
    if standalone:
        media=json.loads(standalone)
        guide=SimpleNamespace(title="Как сменить регион App Store", text=REGION_GUIDE)
        return guide,SimpleNamespace(**media),{}
    ids=[await repo.setting('support_guide_20261108:replies:'+str(i)) for i in (0,4)]
    if not all(v and v.isdigit() for v in ids):raise HTTPException(404,'Инструкция пока не опубликована')
    question,guide=[await session.get(WebQuickReply,int(v)) for v in ids]
    if not question or not guide or not question.enabled or not guide.enabled:raise HTTPException(404,'Инструкция недоступна')
    asset=await session.scalar(select(WebSupportAttachment).where(WebSupportAttachment.quick_reply_id==guide.id,WebSupportAttachment.kind=='video').order_by(WebSupportAttachment.id))
    fields=json.loads(await repo.setting('support_guide_20261108_copy_fields','{}'))
    fields={k:v for k,v in fields.items() if k in ('street','city','region','postcode','phone') and isinstance(v,str) and 0<len(v)<=256}
    return guide,asset,fields

async def region_video_path(session):
    _,asset,_=await published_guide(session)
    if not asset or Path(asset.storage_name).name!=asset.storage_name:
        raise HTTPException(404,'Видео недоступно')
    path=SUPPORT_STORAGE/asset.storage_name
    if not path.is_file():
        raise HTTPException(404,'Видео временно недоступно')
    return path

def materials_router(context):
    router=APIRouter()
    async def authorize(token):
        user=await context.current_user(token)
        if user.status=='blocked':raise HTTPException(403,'Аккаунт заблокирован')
    @router.get('/api/web/support/materials/appstore-region')
    async def article(freeman_session:str|None=Cookie(default=None)):
        await authorize(freeman_session)
        async with context.database.sessions() as s:
            guide,asset,fields=await published_guide(s)
            return JSONResponse(dict(id='appstore-region',title=guide.title,text=REGION_GUIDE,video=dict(url='/api/web/support/materials/appstore-region/video',name=asset.original_name) if asset else None),headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
    @router.get('/api/web/support/materials/appstore-region/video')
    async def video(freeman_session:str|None=Cookie(default=None)):
        await authorize(freeman_session)
        async with context.database.sessions() as s:
            path=await region_video_path(s)
            return FileResponse(path,media_type='video/mp4',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Content-Disposition':'inline'})
    return router
