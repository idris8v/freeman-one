"""Read-only union of AI conversations and legacy tickets; no backfill on GET."""
import json
from types import SimpleNamespace
from sqlalchemy import select,func,or_,case,literal,union_all,cast,String
from app.database.models import AIConversation,AIMessage,AIJob,AIEvent,Ticket,TicketMessage,User
from app.services.freeman_ai.security import aware
from app.web.ai_customer_chat import workspace

async def inbox(session,settings,page,channel,mode,q):
    visible=[AIConversation.channel!='owner',AIConversation.mode!='deleted',select(AIMessage.id).where(AIMessage.conversation_id==AIConversation.id).exists()]
    last_source=select(TicketMessage.source).where(TicketMessage.ticket_id==Ticket.id,TicketMessage.role=='client').order_by(TicketMessage.id.desc()).limit(1).scalar_subquery()
    ticket_channel=case((last_source.like('web:%'),'website'),else_='telegram')
    a=select(literal('ai').label('kind'),AIConversation.id.label('id'),AIConversation.user_id.label('uid'),AIConversation.updated_at.label('at'),AIConversation.mode.label('mode'),AIConversation.channel.label('channel')).where(*visible)
    t=select(literal('ticket'),Ticket.id,Ticket.user_id,Ticket.updated_at,case((Ticket.status=='closed','closed'),else_='human'),ticket_channel).where(select(TicketMessage.id).where(TicketMessage.ticket_id==Ticket.id).exists(),~select(AIConversation.id).where(AIConversation.ticket_id==Ticket.id,AIConversation.channel!='owner',AIConversation.mode!='deleted').exists())
    rows=union_all(a,t).subquery();filters=[]
    if channel:filters.append(rows.c.channel==channel)
    if mode:filters.append(rows.c.mode==mode)
    else:filters.append(rows.c.mode!='closed')
    if q.strip():
        escaped=q.strip()[:100].replace('\\','\\\\').replace('%','\\%').replace('_','\\_');term='%'+escaped+'%'
        users=select(User.id).where(or_(*[column.ilike(term,escape='\\')for column in (User.first_name,User.username,User.account_username,User.email,User.freeman_id)]))
        filters.append(or_(rows.c.uid.in_(users),cast(rows.c.id,String)==q.strip().lstrip('#'),(rows.c.kind=='ai')&rows.c.id.in_(select(AIMessage.conversation_id).where(AIMessage.text.ilike(term,escape='\\'))),(rows.c.kind=='ticket')&rows.c.id.in_(select(TicketMessage.ticket_id).where(TicketMessage.text.ilike(term,escape='\\')))))
    total=await session.scalar(select(func.count()).select_from(rows).where(*filters));page=min(max(1,page),max(1,(total+9)//10));items=[]
    for row in await session.execute(select(rows).where(*filters).order_by(rows.c.at.desc(),rows.c.kind,rows.c.id.desc()).offset((page-1)*10).limit(10)):
        user=await session.get(User,row.uid) if row.uid else None
        model=AIMessage if row.kind=='ai' else TicketMessage;field=model.conversation_id if row.kind=='ai' else model.ticket_id
        last=await session.scalar(select(model).where(field==row.id).order_by(model.id.desc()).limit(1));conv=await session.get(AIConversation,row.id)if row.kind=='ai'else None
        items.append(dict(id=row.id if conv else 'ticket-'+str(row.id),kind=row.kind,avatar=f'/api/web/admin/users/{user.id}/avatar' if user and user.telegram_id else None,name=user.display_name if user else 'Посетитель',freeman_id=user.freeman_id if user else None,channel=row.channel,mode=row.mode,preview=last.text[:120]if last else '',updated_at=aware(row.at).isoformat(),ticket_id=conv.ticket_id if conv else row.id))
    counts=dict((await session.execute(select(rows.c.mode,func.count()).group_by(rows.c.mode))).all())
    failures=await session.scalar(select(func.count()).select_from(AIJob).where(AIJob.status=='failed',AIJob.message_id.in_(select(AIMessage.id).where(AIMessage.conversation_id.in_(select(AIConversation.id).where(*visible))))))
    usage=0
    for event in await session.scalars(select(AIEvent).where(AIEvent.kind=='model.usage',AIEvent.conversation_id.in_(select(AIConversation.id).where(AIConversation.channel!='owner')))):
        try:usage+=int(json.loads(event.details).get('total_tokens',0))
        except (ValueError,TypeError,AttributeError):pass
    from app.services.freeman_ai.customer_config import status
    profile=await status(session,settings)
    return dict(items=items,total=total,page=page,stats=dict(total=sum(counts.values()),ai=counts.get('ai',0),human=counts.get('human',0),closed=counts.get('closed',0),failures=failures,total_tokens=usage,cost=None),enabled=profile['enabled'],configured=profile['configured'],customer_api=profile,whatsapp_configured=False,telegram_business='not_enabled',telegram_enabled=profile['telegram_enabled'],whatsapp_enabled=False)

async def ticket_workspace(session,settings,ticket,viewer):
    fake=SimpleNamespace(id=0,user_id=ticket.user_id,channel='website',identity_id=None,ticket_id=ticket.id,mode='closed'if ticket.status=='closed'else'human')
    result=await workspace(session,settings,fake,viewer,[])
    return dict(result,id='ticket-'+str(ticket.id),kind='ticket',ticket_id=ticket.id,mode=fake.mode,channel='website')
