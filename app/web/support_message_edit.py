"""Own website messages: 30-minute editing, optimistic version, no new sends."""
import json,hashlib
from datetime import timezone,timedelta
from fastapi import APIRouter,Cookie,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select
from app.database.models import Ticket,TicketMessage,WebSupportAttachment,AppSetting,AIConversation,AIMessage,ChatNotice,now
from app.database.repositories import SQLRepository
from app.services.freeman_ai.security import fingerprint,chat_text as redact
class EditPayload(BaseModel):
    text:str=Field(max_length=3500,strict=True)
    expected_version:int=Field(ge=0,strict=True)
def stamp(value):return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
def allowed(settings,user,ticket,message):
    age=stamp(now())-stamp(message.created_at)
    return (user.status!='blocked' and timedelta(0)<=age<=timedelta(minutes=30)
        and message.source.startswith('web:'+str(user.id)+':')
        and (message.role=='client' and ticket.user_id==user.id or message.role=='admin' and settings.can_admin_action(user.telegram_id,'admin:t:home')))
async def message_edit_meta(session,settings,viewer,ticket,messages):
    keys=['support_message_edit:'+str(m.id) for m in messages]
    versions={}
    if keys:
        for k,v in await session.execute(select(AppSetting.key,AppSetting.value).where(AppSetting.key.in_(keys))):
            try:versions[int(k.rsplit(':',1)[1])]=json.loads(v)
            except (ValueError,TypeError):pass
    return {m.id:dict(edit_id=m.id if settings and allowed(settings,viewer,ticket,m) else None,edit_version=versions.get(m.id,{}).get('version',0),edit_until=(stamp(m.created_at)+timedelta(minutes=30)).isoformat(),edited=bool(versions.get(m.id,{}).get('version'))) for m in messages}
def edit_router(context):
    router=APIRouter()
    @router.patch('/api/web/support/messages/{message_id}')
    async def edit(message_id:int,payload:EditPayload,freeman_session:str|None=Cookie(default=None)):
        user=await context.current_user(freeman_session)
        if user.status=='blocked':raise HTTPException(403,'Аккаунт заблокирован')
        async with context.database.sessions.begin() as s:
            message=await s.get(TicketMessage,message_id,with_for_update=True)
            ticket=await s.get(Ticket,message.ticket_id) if message else None
            if not ticket or ticket.user_id!=user.id and not context.settings.can_admin_action(user.telegram_id,'admin:t:home'):raise HTTPException(404,'Сообщение недоступно')
            if not allowed(context.settings,user,ticket,message):raise HTTPException(403,'Можно изменять только свои сообщения сайта в течение 30 минут после отправки')
            repo=SQLRepository(s);key='support_message_edit:'+str(message.id)
            previous=json.loads(await repo.setting(key,'{}'));version=previous.get('version',0)
            text=payload.text.strip()
            if version!=payload.expected_version:
                if version==payload.expected_version+1 and text==message.text:return dict(ok=True,text=message.text,version=version,edited_at=previous.get('at'))
                raise HTTPException(409,'Сообщение уже изменено. Обновите диалог перед новой правкой.')
            if not text and not message.photo and not message.document and not await s.scalar(select(WebSupportAttachment.id).where(WebSupportAttachment.message_id==message.id).limit(1)):raise HTTPException(422,'Введите текст сообщения')
            mirrors=[]
            for conv in await s.scalars(select(AIConversation).where(AIConversation.ticket_id==ticket.id,AIConversation.user_id==ticket.user_id)):
                row=await s.scalar(select(AIMessage).where(AIMessage.conversation_id==conv.id,AIMessage.source_hash==fingerprint(context.settings,'message:'+conv.channel+':ticket:'+str(message.id))))
                if row:
                    if conv.mode=='ai' and row.role=='client' and row.generation==conv.generation and not await s.scalar(select(AIMessage.id).where(AIMessage.conversation_id==conv.id,AIMessage.generation==row.generation,AIMessage.role=='assistant').limit(1)):raise HTTPException(409,'Дождитесь ответа помощника, затем исправьте сообщение.')
                    mirrors.append(row)
            if text==message.text:return dict(ok=True,text=text,version=version,edited_at=previous.get('at'))
            old_hash=hashlib.sha256(message.text.encode()).hexdigest();message.text=text
            for row in mirrors:row.text=redact(text)
            for notice in await s.scalars(select(ChatNotice).where(ChatNotice.ticket_id==ticket.id,ChatNotice.status=='pending',ChatNotice.kind=='client',ChatNotice.dedup.like('ticket.message:'+str(message.id)+':%'))):notice.text=text
            version+=1;at=stamp(now()).isoformat();await repo.set_setting(key,json.dumps(dict(version=version,at=at,actor_id=user.id)))
            await repo.set_setting('support_message_edit_audit:'+str(message.id)+':'+str(version),json.dumps(dict(actor_id=user.id,at=at,before=old_hash,after=hashlib.sha256(text.encode()).hexdigest())))
        return dict(ok=True,text=text,version=version,edited_at=at)
    return router
