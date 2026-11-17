"""Explicit, authorized and monotonic read acknowledgements. No GET side effects."""
from fastapi import APIRouter,Cookie,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select
from app.database.models import AppSetting,Ticket,TicketMessage
from app.database.repositories import SQLRepository
class ReadPayload(BaseModel):
    message_id:int=Field(gt=0,strict=True)
async def read_positions(session,ticket):
    positions={'client':0,'staff':0}
    for key,value in await session.execute(select(AppSetting.key,AppSetting.value).where(AppSetting.key.like('support_read:%:'+str(ticket.id)))):
        parts=key.split(':')
        if len(parts)==3 and parts[1].isdigit() and value.isdigit():
            role='client' if int(parts[1])==ticket.user_id else 'staff';positions[role]=max(positions[role],int(value))
    return positions

def receipts_router(context):
    router=APIRouter()
    @router.post('/api/web/support/tickets/{ticket_id}/read')
    async def acknowledge(ticket_id:int,payload:ReadPayload,freeman_session:str|None=Cookie(default=None)):
        user=await context.current_user(freeman_session)
        if user.status=='blocked':raise HTTPException(403,'Аккаунт заблокирован')
        async with context.database.sessions.begin() as s:
            ticket=await s.get(Ticket,ticket_id,with_for_update=True)
            if not ticket or ticket.user_id!=user.id and not context.settings.can_admin_action(user.telegram_id,'admin:t:home'):raise HTTPException(404,'Обращение недоступно')
            message=await s.get(TicketMessage,payload.message_id)
            if not message or message.ticket_id!=ticket.id:raise HTTPException(404,'Сообщение недоступно')
            repo=SQLRepository(s);key='support_read:'+str(user.id)+':'+str(ticket.id);old=await repo.setting(key,'0');value=max(payload.message_id,int(old) if old.isdigit() else 0)
            await repo.set_setting(key,str(value))
        return {'ok':True,'read_to':value}
    return router
