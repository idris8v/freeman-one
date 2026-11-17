import json
from datetime import timedelta
from sqlalchemy import select,func,update
from app.database.models import AIConversation,AIMessage,AIJob,AIOutbox,AIEvent,Ticket,TicketMessage,User,now
from app.database.repositories import SQLRepository
from app.services.customer_identity import lock_nonce
from .security import redact,chat_text,URL,fingerprint,aware
from .identity import customer

async def enabled(session,settings):return settings.freeman_ai_enabled and await SQLRepository(session).setting('freeman_ai_enabled','0')=='1'

async def channel_enabled(session,settings,channel):
    if channel=='owner':return await enabled(session,settings)
    from .customer_config import enabled as customer_enabled
    return channel in ('website','telegram') and await customer_enabled(session,settings) and (channel!='telegram' or getattr(settings,'freeman_customer_ai_telegram_enabled',False))

async def lock_ticket(session,conv):
    if conv.channel!='owner' and conv.ticket_id:
        await session.execute(update(Ticket).where(Ticket.id==conv.ticket_id).values(updated_at=Ticket.updated_at))

async def ticket_status(session,settings,item):
    rows=list(await session.scalars(select(AIConversation).where(AIConversation.ticket_id==item.id,AIConversation.channel!='owner',AIConversation.mode!='deleted').with_for_update()))
    for conv in rows:
        mode='closed' if item.status=='closed' else 'human'
        if conv.mode!=mode:conv.mode=mode;conv.generation+=1;conv.updated_at=now()

async def attach_ticket(session,settings,item,source,force=False):
    """Only explicit incoming ticket text can start an enabled customer agent."""
    if item.status=='closed' or not await channel_enabled(session,settings,'website'):return None
    await session.execute(update(Ticket).where(Ticket.id==item.id).values(updated_at=Ticket.updated_at))
    existing=await session.scalar(select(AIConversation).where(AIConversation.ticket_id==item.id,AIConversation.channel!='owner',AIConversation.mode!='deleted').order_by(AIConversation.id.desc()).limit(1))
    if existing:return existing
    if not force and await session.scalar(select(func.count()).select_from(TicketMessage).where(TicketMessage.ticket_id==item.id))!=1:return None
    message=await session.scalar(select(TicketMessage).where(TicketMessage.source==source,TicketMessage.ticket_id==item.id,TicketMessage.role=='client'))
    if not message:return None
    # Bot messages require their independently enabled channel; web tickets stay website-bound.
    channel='website' if source.startswith('web:') else 'telegram'
    if not await channel_enabled(session,settings,channel):return None
    identity_id=None
    if channel=='telegram':
        user=await session.get(User,item.user_id)
        if not user or not user.telegram_id or user.telegram_id<=0:return None
        from .identity import channel_identity
        identity=await channel_identity(session,settings,'telegram',str(user.telegram_id))
        verified=await customer(session,identity)
        if not verified or verified.id!=item.user_id:return None
        identity_id=identity.id
    conv=AIConversation(user_id=item.user_id,channel=channel,identity_id=identity_id,ticket_id=item.id,mode='ai');session.add(conv);await session.flush()
    await append(session,settings,conv,message.text or 'Клиент приложил файл. Если нужна проверка вложения, передай специалисту.','ticket:'+str(message.id),mirror_ticket=False)
    return conv

async def conversation(session,settings,*,user_id=None,browser=None,identity=None):
    if identity:
        user=await customer(session,identity);user_id=user.id if user else None
        filters=[AIConversation.identity_id==identity.id,AIConversation.user_id==user_id]
        channel=identity.channel;lock='channel:'+str(identity.id)
    else:
        channel='website';digest=fingerprint(settings,'browser:'+browser) if browser else None
        if not user_id and not digest:raise PermissionError('Conversation identity required')
        filters=[AIConversation.channel=='website',AIConversation.user_id==user_id]
        if not user_id:filters.append(AIConversation.browser_hash==digest)
        lock='web:'+str(user_id or digest)
    await lock_nonce(session,'ai:conversation:'+lock)
    row=await session.scalar(select(AIConversation).where(*filters,AIConversation.mode!='closed').order_by(AIConversation.id.desc()).limit(1))
    if not row:
        row=AIConversation(user_id=user_id,identity_id=identity.id if identity else None,channel=channel,browser_hash=None if identity or user_id else digest);session.add(row);await session.flush()
    return row

async def append(session,settings,conv,text,source,role='client',actions=None,mirror_ticket=True):
    if conv.channel=='owner':await lock_nonce(session,'ai:scheduled-conversation:'+str(conv.id))
    else:await lock_ticket(session,conv)
    if conv.mode=='closed':raise ValueError('Conversation closed')
    source_hash=fingerprint(settings,'message:'+conv.channel+':'+source)
    await lock_nonce(session,'ai:message:'+source_hash)
    existing=await session.scalar(select(AIMessage).where(AIMessage.source_hash==source_hash))
    if existing:
        if existing.conversation_id!=conv.id:raise PermissionError('Message context changed')
        return existing
    await session.execute(update(AIConversation).where(AIConversation.id==conv.id).values(updated_at=AIConversation.updated_at))
    await session.refresh(conv)
    if conv.mode=='closed':raise ValueError('Conversation closed')
    old_ticket_id=conv.ticket_id
    recent=await session.scalar(select(func.count()).select_from(AIMessage).where(AIMessage.conversation_id==conv.id,AIMessage.role=='client',AIMessage.created_at>now()-timedelta(minutes=1)))
    if role=='client' and recent>=10:raise ValueError('Слишком много сообщений. Подождите минуту.')
    safe=chat_text(text);extras=actions or []
    if role=='client' and conv.channel!='owner':
        from .tools import key_diagnostic
        candidates=URL.findall(text)
        if candidates:extras=[{'type':'diagnostic','data':await key_diagnostic(session,candidates[0])}]
    msg=AIMessage(conversation_id=conv.id,source_hash=source_hash,role=role,generation=conv.generation,text=safe,actions=json.dumps(extras,ensure_ascii=False));session.add(msg);await session.flush()
    conv.updated_at=now()
    if role=='client':
        conv.generation+=1
        conv.specialist='support' if any(x in text.casefold() for x in ('не работает','ошибк','подключ','ключ','оплатил','поддержк')) else conv.specialist
        from .dialogue import explicit_handoff
        human_request=explicit_handoff(text)
        from .v21.integration import active as v21_active
        if human_request and conv.channel!='owner' and not await v21_active(session):
            await handoff(session,settings,conv,'customer_requested')
            await append(session,settings,conv,'Передаю обращение оператору. Переписка сохранена.','ack:'+str(msg.id),'system')
        elif conv.mode=='ai':session.add(AIJob(message_id=msg.id,generation=conv.generation))
        if conv.mode=='human' and old_ticket_id and mirror_ticket:
            session.add(TicketMessage(ticket_id=conv.ticket_id,role='client',text=safe[:3500],source='ai:'+str(msg.id)))
            ticket=await session.get(Ticket,conv.ticket_id);ticket.status='waiting_admin';ticket.updated_at=now()
    elif role in ('assistant','operator','system') and conv.channel in ('telegram','whatsapp'):session.add(AIOutbox(message_id=msg.id))
    if role=='assistant' and conv.channel!='owner' and conv.ticket_id:
        ticket=await session.get(Ticket,conv.ticket_id)
        if ticket and ticket.status!='closed':
            official = {
                'https://t.me/FreemanOneBot': 'Открыть новый бот',
                'https://freemanone.pro/buy': 'Купить без регистрации',
            }
            links = [f"[{official[url]}]({url})" for url in official
                     if any(action.get('type')=='link' and action.get('url')==url for action in extras)]
            tail = '\n\n'+'\n\n'.join(links) if links else ''
            session.add(TicketMessage(ticket_id=ticket.id,role='admin',text=safe[:3500-len(tail)]+tail,source='ai:'+str(msg.id)))
            waiting = await SQLRepository(session).setting(f'agent_v21_waiting:{conv.id}') == '1'
            ticket.status = 'waiting_admin' if waiting else 'waiting_client'
            ticket.updated_at = now()
    return msg

async def handoff(session,settings,conv,reason):
    if conv.mode=='human':return
    await lock_ticket(session,conv)
    conv.mode='human';conv.generation+=1;conv.updated_at=now()
    if conv.user_id and not conv.ticket_id:
        from app.services.tickets import TicketService
        repo=SQLRepository(session);user=await session.get(User,conv.user_id)
        messages=list(await session.scalars(select(AIMessage).where(AIMessage.conversation_id==conv.id).order_by(AIMessage.id.desc()).limit(12)))
        summary='Freeman AI · '+conv.channel+'\nПричина: '+redact(reason)+'\n\n'+'\n'.join(m.role+': '+m.text for m in reversed(messages))
        source='ai:handoff:'+str(conv.id)
        await TicketService(repo,settings).input('t:input:new',user,summary[:3400],source,force_new=True)
        message=await session.scalar(select(TicketMessage).where(TicketMessage.source==source));conv.ticket_id=message.ticket_id
    if conv.ticket_id:
        ticket=await session.get(Ticket,conv.ticket_id)
        if ticket and ticket.status!='closed':ticket.status='waiting_admin';ticket.updated_at=now()
    session.add(AIEvent(conversation_id=conv.id,kind='handoff',name='human',details=json.dumps({'reason':redact(reason)[:300]})))

async def switch_mode(session,settings,conv,mode):
    await lock_ticket(session,conv)
    if mode not in ('ai','human','closed'):raise ValueError('Invalid mode')
    await session.execute(update(AIConversation).where(AIConversation.id==conv.id).values(updated_at=AIConversation.updated_at));await session.refresh(conv)
    if mode=='human':await handoff(session,settings,conv,'operator_takeover')
    else:conv.mode=mode;conv.generation+=1;conv.updated_at=now()
    if conv.channel!='owner' and conv.ticket_id:
        ticket=await session.get(Ticket,conv.ticket_id)
        if ticket:
            if mode=='closed':ticket.status='closed'
            elif ticket.status=='closed':ticket.status='waiting_admin'
            ticket.updated_at=now()
    if conv.channel!='owner':
        await SQLRepository(session).set_setting(f'agent_v21_waiting:{conv.id}','0')
        from .v21.state import StateStore
        store=StateStore(session,settings);state=await store.load(conv)
        if mode=='ai':state.return_to_agent()
        elif mode=='closed':state.phase='completed'
        else:state.operator_reply()
        state.generation=conv.generation
        await store.save(conv,state,conv.generation)
    session.add(AIEvent(conversation_id=conv.id,kind='operator.mode',name=mode))

async def messages(session,conv):
    rows=await session.scalars(select(AIMessage).where(AIMessage.conversation_id==conv.id).order_by(AIMessage.id.desc()).limit(100))
    result=[]
    from app.database.models import AIOwnerImage,AppSetting
    from collections import defaultdict
    ordered=list(reversed(list(rows)));ids=[m.id for m in ordered]
    image_map=defaultdict(list);file_map={};completed={}
    if conv.channel=='owner' and ids:
        from .files import present_many
        file_map=await present_many(session,ids)
        for mid,iid in await session.execute(select(AIOwnerImage.message_id,AIOwnerImage.id).where(AIOwnerImage.message_id.in_(ids)).order_by(AIOwnerImage.id)):
            image_map[mid].append(iid)
        keys=[f'ai_site_done:{m.id}:{i}' for m in ordered for i,a in enumerate(json.loads(m.actions)) if a.get('type')=='site_change']
        if keys:completed=dict((await session.execute(select(AppSetting.key,AppSetting.value).where(AppSetting.key.in_(keys)))).all())
    for m in ordered:
        images=image_map[m.id];files=file_map.get(m.id,[])
        public_actions=json.loads(m.actions)
        if conv.channel=='owner':
            from app.web.posts import load_json
            for index,action in enumerate(public_actions):
                if action.get('type')=='site_change':
                    key=f'ai_site_done:{m.id}:{index}';raw=completed.get(key)
                    done=json.loads(raw) if raw else None
                    if isinstance(done,dict) and '_chunks' in done:done=await load_json(SQLRepository(session),key)
                    action['completed']=bool(done)
        blocks=[{'type':'markdown','text':m.text}]+files+[{'type':'image','url':'/api/web/admin/assistant/images/'+str(i)} for i in images]+[a['block'] for a in json.loads(m.actions) if a.get('type')=='rich_block' and conv.channel=='owner']
        result.append(dict(blocks=blocks,files=files,id=m.id,role=m.role,text=m.text,created_at=aware(m.created_at).isoformat(),images=['/api/web/admin/assistant/images/'+str(i) for i in images],actions=[a for a in public_actions if a.get('type') in ('link','copy','agent_action','agent_image') or (conv.channel=='owner' and a.get('type') in ('knowledge_proposal','knowledge_saved','rich_block','broadcast_proposal','site_change'))]))
    return result
