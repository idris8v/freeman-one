"""Customer AI chat presentation; replies reuse the existing support transaction/outbox."""
import hashlib,json
from types import SimpleNamespace
from fastapi import HTTPException
from sqlalchemy import select
from app.database.models import (AIConversation, AIMessage, User, Ticket,
    TicketMessage, WebQuickReply, WebSupportAttachment, AccessKey, ChatNotice, ChannelIdentity, Subscription, Tariff, VlessCredential, VpnCredential, AppSetting)
from app.database.repositories import SQLRepository
from app.services.freeman_ai import service
from app.services.freeman_ai.security import aware, fingerprint, decrypt
from app.services.freeman_ai.identity import customer
from app.services.accounts import display_freeman_id
from app.web.cabinet.schemas import SupportMessagePayload
from app.web.cabinet.support_presenter import attachment_json, ticket_json
from app.web.cabinet.support_storage import safe_attachment


async def telegram_target(session, settings, conv):
    """Use the verified conversation identity, never a submitted customer/chat ID."""
    identity = await session.get(ChannelIdentity, conv.identity_id) if conv.identity_id else None
    owner = await customer(session, identity) if identity else None
    if not identity or identity.channel != 'telegram' or not owner or owner.id != conv.user_id:
        return None
    try:
        target = int(decrypt(settings, identity.encrypted_subject))
        return target if target > 0 and target == owner.telegram_id else None
    except (ValueError, TypeError):
        return None


async def workspace(session, settings, conv, viewer, messages):
    user = await session.get(User, conv.user_id) if conv.user_id else None
    media = bool(user and conv.channel == 'website')
    if user and conv.channel == 'telegram':
        media = bool(await telegram_target(session, settings, conv))
    client = None
    if user:
        from app.services.client_registry import active_key
        can_view_subscriptions=settings.is_owner(viewer.telegram_id)
        keys = (await session.execute(select(AccessKey.id, AccessKey.name, AccessKey.expires_at,
                    active_key().label('active')).where(AccessKey.user_id == user.id,
                    AccessKey.status != 'deleted').order_by(AccessKey.expires_at.desc()).limit(5))).all() if can_view_subscriptions else []
        ids=[k.id for k in keys];links={}
        if ids:
            for model in (VlessCredential,VpnCredential):
                links.update(dict((await session.execute(select(model.access_key_id,model.subscription_id).where(model.access_key_id.in_(ids)))).all()))
            for key,value in await session.execute(select(AppSetting.key,AppSetting.value).where(AppSetting.key.startswith('subscription_key:'),AppSetting.value.in_([str(v) for v in ids]))):
                suffix=key.split(':')[-1]
                if suffix.isdigit():links[int(value)]=int(suffix)
        subs={v.id:v for v in await session.scalars(select(Subscription).where(Subscription.id.in_(list(links.values())),Subscription.user_id==user.id))} if links else {}
        tariffs={v.id:v for v in await session.scalars(select(Tariff).where(Tariff.id.in_([sub.tariff_id for sub in subs.values() if sub.tariff_id])))} if subs else {}
        def plan(k):
            sub=subs.get(links.get(k.id));tariff=tariffs.get(sub.tariff_id) if sub else None
            return dict(id=k.id,name=k.name,expires_at=aware(k.expires_at).isoformat(),active=bool(k.active),months=tariff.months if tariff else None,device_limit=sub.device_limit if sub else None)
        client = dict(id=user.id, name=user.display_name, freeman_id=display_freeman_id(user.freeman_id) or f'FO-{user.id}',
            username=user.username, email=user.email, status=user.status,
            created_at=aware(user.created_at).isoformat(),telegram_id=user.telegram_id,
            balance=await SQLRepository(session).balance(user.id) if can_view_subscriptions else None,
            avatar=f'/api/web/admin/users/{user.id}/avatar' if user.telegram_id else None,
            can_open_profile=can_view_subscriptions,can_view_subscriptions=can_view_subscriptions,
            subscriptions=[plan(k) for k in keys])
    replies=[]
    quick=list(await session.scalars(select(WebQuickReply).where(WebQuickReply.enabled == True).order_by(WebQuickReply.position,WebQuickReply.id).limit(100)))
    from app.web.cabinet.support_library import customer_ids
    help_ids = await customer_ids(session)
    quick = [q for q in quick if q.id not in help_ids]
    quick_assets={}
    if quick:
        for asset in await session.scalars(select(WebSupportAttachment).where(WebSupportAttachment.quick_reply_id.in_([q.id for q in quick]))):
            quick_assets.setdefault(asset.quick_reply_id,[]).append(dict(name=asset.original_name,kind=asset.kind,**({'url':f'/api/web/support/attachments/{asset.id}'} if settings.is_owner(viewer.telegram_id) else {})))
    for q in quick:replies.append(dict(id=q.id,title=q.title,text=q.text,attachments=quick_assets.get(q.id,[])))
    ticket_closed,result=await timeline(session,settings,conv,viewer,messages)
    return dict(workspace=True,client=client,quick_replies=replies,messages=result,
        capabilities=dict(attachments=media,dictation=media,support_reply=media,reply=media and conv.mode!='closed' and not ticket_closed,reason='Обращение закрыто.' if ticket_closed else '' if media else 'Нет подтверждённого получателя: история доступна для просмотра. Ответы доступны зарегистрированному клиенту сайта или подтверждённому Telegram.'))


async def timeline(session,settings,conv,viewer,messages):
    ticket_closed=False
    result=[dict(m,attachments=[],delivery='saved' if m['role'] in ('operator','assistant') else None) for m in messages]
    from app.web.support_message_actions import web_actions
    from app.web.support_materials import guide_attachment
    raw_actions=dict((await session.execute(select(AIMessage.id,AIMessage.actions).where(AIMessage.conversation_id==conv.id,AIMessage.id.in_([m['id'] for m in messages])))).all())
    for item in result:
        actions=raw_actions.get(item['id'],item.get('actions',[]))
        item['actions']=web_actions(item.get('text',''),actions) if item.get('role') in ('assistant','operator') else []
        if item['role']=='assistant':
            item['attachments']=guide_attachment(actions)
    # Map mirrored ticket messages back to their AI message identity, without duplicating history.
    if conv.ticket_id:
        ticket=await session.get(Ticket,conv.ticket_id)
        if ticket and ticket.user_id==conv.user_id:
            ticket_closed=ticket.status=='closed'
            from app.web.support_receipts import read_positions
            positions=await read_positions(session,ticket);read_to=positions['client']
            rows=list(await session.scalars(select(TicketMessage).where(TicketMessage.ticket_id==ticket.id).order_by(TicketMessage.id.desc()).limit(100)))
            from app.web.support_message_edit import message_edit_meta
            edit_meta=await message_edit_meta(session,settings,viewer,ticket,rows)
            ids=[m.id for m in rows]; grouped={}
            for a in await session.scalars(select(WebSupportAttachment).where(WebSupportAttachment.message_id.in_(ids))):
                grouped.setdefault(a.message_id,[]).append(attachment_json(a))
            sources=dict((await session.execute(select(AIMessage.id,AIMessage.source_hash).where(AIMessage.conversation_id==conv.id,AIMessage.id.in_([m['id'] for m in messages])))).all())
            lookup={v:k for k,v in sources.items()}; byid={m['id']:m for m in result}
            jobs=list(await session.scalars(select(ChatNotice).where(ChatNotice.ticket_id==ticket.id,ChatNotice.kind=='client',ChatNotice.dedup.like('ticket.message:%')).order_by(ChatNotice.id.desc()).limit(200)))
            for row in rows:
                if row.source.startswith('ai:handoff:'):continue
                mid=int(row.source[3:]) if row.source.startswith('ai:') and row.source[3:].isdigit() else lookup.get(fingerprint(settings,'message:'+conv.channel+':ticket:'+str(row.id)))
                item=byid.get(mid)
                if item is None:
                    item=dict(id='ticket-'+str(row.id),role='operator' if row.role=='admin' else row.role,text=row.text,created_at=aware(row.created_at).isoformat(),attachments=[])
                    result.append(item)
                # The ticket is authoritative for human messages. Older AI
                # mirrors may irreversibly redact URLs; don't replace the
                # original customer/operator message with that lossy copy.
                item['text'] = row.text
                item['actions'] = web_actions(row.text, raw_actions.get(mid, [])) if item.get('role') in ('assistant','operator') else []
                item.update(edit_meta.get(row.id,{}))
                item['read_id']=row.id
                item['attachments']=grouped.get(row.id,[])+(
                    guide_attachment(raw_actions.get(mid,[])) if item['role']=='assistant' else [])
                for kind in ('photo','document'):
                    if getattr(row,kind):item['attachments'].append(dict(kind='image' if kind=='photo' else 'document',name='Файл из Telegram',url=f'/api/web/support/messages/{row.id}/media/{kind}'))
                if row.source.startswith(f'web:{viewer.id}:'):item['client_request_id']=row.source.split(':',2)[2]
                # The installed bot delivers a chat-link notification, not the file.
                # Asset jobs are intentionally cancelled by its current policy.
                relevant=[j.status for j in jobs if j.dedup.startswith(f'ticket.message:{row.id}:')]
                if row.role=='admin':
                    # The message is already durable. Telegram notices are a
                    # separate side channel; cancelled media notices are normal.
                    item['notification_delivery']=('failed' if 'failed' in relevant else 'queued' if any(v in ('pending','sending') for v in relevant) else 'sent' if 'sent' in relevant else 'not_required')
                    item['delivery']='saved'
                if row.role=='admin' and read_to>=row.id:item['delivery']='read'
                if row.role=='client':item['delivery']='read' if positions['staff']>=row.id else 'saved'
            result.sort(key=lambda m:(m['created_at'],str(m['id'])))
    return ticket_closed,result


def reply_handler(database, settings, current_user):
    # Reuse the exact existing endpoint, including validation, row locking,
    # fingerprint/idempotency checks, persistence and durable delivery queue.
    from app.web.cabinet.support import support_router
    support = support_router(SimpleNamespace(database=database,settings=settings,current_user=current_user))
    send = next(r.endpoint for r in support.routes if r.path=='/api/web/support/messages')
    async def reply(cid,payload,actor,token):
        if payload.client_mode or payload.ticket_id is not None:
            raise HTTPException(422,'Обращение определяется по выбранному диалогу.')
        for asset in payload.attachments:safe_attachment(asset)
        if sum(len(a.data)*3//4 for a in payload.attachments)>40*1024*1024:
            raise HTTPException(422,'Общий размер вложений — до 40 МБ.')
        async with database.sessions.begin() as session:
            conv=await session.scalar(select(AIConversation).where(AIConversation.id==cid).with_for_update())
            if not conv or conv.channel=='owner' or conv.mode=='deleted':raise HTTPException(404,'Диалог недоступен.')
            if not conv.user_id:raise HTTPException(409,'Для отправки вложений клиент должен быть привязан к аккаунту.')
            existing=await session.scalar(select(TicketMessage).where(TicketMessage.source==f'web:{actor.id}:{payload.request_id}'))
            if existing and existing.ticket_id!=conv.ticket_id:raise HTTPException(409,'Идентификатор уже использован в другом диалоге.')
            if existing:
                ticket=await session.get(Ticket,existing.ticket_id)
                if not ticket or ticket.user_id!=conv.user_id:raise HTTPException(409,'Связь обращения с клиентом не подтверждена.')
                canonical=payload.model_copy(update={'ticket_id':conv.ticket_id})
                expected=hashlib.sha256(json.dumps(canonical.model_dump(exclude={'request_id'}),sort_keys=True).encode()).hexdigest()
                if await SQLRepository(session).setting(f'support_request:web:{actor.id}:{payload.request_id}')!=expected:raise HTTPException(409,'Идентификатор уже использован для другого сообщения.')
                return await ticket_json(session,ticket,actor,True,True)
            if conv.mode=='closed':raise HTTPException(409,'Диалог закрыт. Сообщение не отправлено.')
            quick=await session.get(WebQuickReply,payload.quick_reply_id) if payload.quick_reply_id else None
            from app.web.cabinet.support_library import customer_ids
            if payload.quick_reply_id and (not quick or not quick.enabled or quick.id in await customer_ids(session)):raise HTTPException(404,'Быстрый ответ недоступен.')
            if quick:
                from app.web.cabinet import support_storage
                for asset in await session.scalars(select(WebSupportAttachment).where(WebSupportAttachment.quick_reply_id==quick.id)):
                    if asset.kind=='audio':raise HTTPException(422,'Голосовые сообщения отключены. Используйте голосовой ввод в текст.')
                    if not (support_storage.SUPPORT_STORAGE/asset.storage_name).is_file():raise HTTPException(409,'Файл быстрого ответа отсутствует. Выберите другой ответ.')
            has_media=bool(payload.attachments) or bool(quick and await session.scalar(select(WebSupportAttachment.id).where(WebSupportAttachment.quick_reply_id==quick.id).limit(1)))
            if conv.channel not in ('website','telegram') or (conv.channel=='telegram' and not await telegram_target(session,settings,conv)):
                raise HTTPException(409,'Ответ через поддержку недоступен для этого канала. Используйте существующий текстовый ответ.')
            if not payload.text.strip() and not has_media and not quick:raise HTTPException(422,'Введите сообщение или добавьте вложение.')
            if not conv.ticket_id:
                if conv.mode!='human':await service.handoff(session,settings,conv,'operator_reply')
                else:
                    from app.services.tickets import TicketService
                    user=await session.get(User,conv.user_id);source='ai:handoff:'+str(conv.id)
                    await TicketService(SQLRepository(session),settings).input('t:input:new',user,'Freeman AI · история диалога #'+str(cid),source,force_new=True)
                    initial=await session.scalar(select(TicketMessage).where(TicketMessage.source==source));conv.ticket_id=initial.ticket_id
            ticket=await session.get(Ticket,conv.ticket_id)
            if not ticket or ticket.user_id!=conv.user_id:raise HTTPException(409,'Связь обращения с клиентом не подтверждена.')
            tid=ticket.id
        return await send(payload.model_copy(update={'ticket_id':tid}),freeman_session=token)
    return reply
