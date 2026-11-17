from app.services.accounts import display_freeman_id
"""Support response mapping; database reads remain in the caller transaction."""

from datetime import timezone

from sqlalchemy import func, select

from app.database.models import TicketMessage, User, WebSupportAttachment
from app.database.repositories import SQLRepository
from app.services.tickets import STATUSES


def attachment_json(item):
    return {
        "id": item.id,
        "kind": item.kind,
        "mime_type": item.mime_type,
        "name": item.original_name,
        "size": item.size,
        "url": f"/api/web/support/attachments/{item.id}",
    }


async def ticket_json(session, item, viewer, is_admin=False, include_messages=False, settings=None):
    user = await session.get(User, item.user_id)
    first_message = await session.scalar(
        select(TicketMessage)
        .where(TicketMessage.ticket_id == item.id)
        .order_by(TicketMessage.id)
        .limit(1)
    )
    subject = (
        " ".join((first_message.text if first_message else "").split())[:72]
        or f"Обращение #{item.id}"
    )
    result = {
        "id": item.id,
        "status": item.status,
        "subject": subject,
        "created_at": item.created_at.replace(tzinfo=timezone.utc).isoformat(),
        "updated_at": item.updated_at.replace(tzinfo=timezone.utc).isoformat(),
        "client": {
            "id": user.id,
            "freeman_id": display_freeman_id(user.freeman_id) or f"FO-{user.id}",
            "telegram_id": user.telegram_id,
            "name": user.display_name,
            "username": user.username,
            "email": user.email,
            "avatar": f"/api/web/admin/users/{user.id}/avatar"
            if user.telegram_id
            else None,
        }
        if is_admin
        else None,
    }
    repo = SQLRepository(session)
    result["pinned"] = await repo.setting(f"support_pinned:{item.id}", "0") == "1"
    result["important"] = await repo.setting(f"support_important:{item.id}", "0") == "1"
    latest_message = await session.scalar(
        select(TicketMessage)
        .where(TicketMessage.ticket_id == item.id)
        .order_by(TicketMessage.id.desc())
        .limit(1)
    )
    result["last_message"] = latest_message.text[:140] if latest_message else ""
    latest_client = await session.scalar(
        select(TicketMessage)
        .where(TicketMessage.ticket_id == item.id, TicketMessage.role == "client")
        .order_by(TicketMessage.id.desc())
        .limit(1)
    )
    result["channel"] = (
        "site"
        if latest_client and latest_client.source.startswith("web:")
        else "telegram"
        if latest_client
        else None
    )
    read = int(await repo.setting(f"support_read:{viewer.id}:{item.id}", "0") or 0)
    incoming = (
        TicketMessage.role == "client"
        if is_admin
        else TicketMessage.role.in_(("admin", "system"))
    )
    result["unread"] = await session.scalar(
        select(func.count())
        .select_from(TicketMessage)
        .where(TicketMessage.ticket_id == item.id, TicketMessage.id > read, incoming)
    )
    result["status_label"] = STATUSES.get(item.status, item.status)
    from app.database.models import AIConversation
    conversation=await session.scalar(select(AIConversation).where(AIConversation.ticket_id==item.id,AIConversation.channel!='owner',AIConversation.mode!='deleted').order_by(AIConversation.id.desc()).limit(1))
    result['ai_conversation_id']=conversation.id if conversation else None
    result['routing_mode']=conversation.mode if conversation else 'human'
    if include_messages:
        from app.web.support_receipts import read_positions
        reads=await read_positions(session,item)
        messages = list(
            await session.scalars(
                select(TicketMessage)
                .where(TicketMessage.ticket_id == item.id)
                .order_by(TicketMessage.id)
            )
        )
        from app.web.support_message_edit import message_edit_meta
        edit_meta=await message_edit_meta(session,settings,viewer,item,messages)
        ids = [message.id for message in messages]
        attachments = (
            list(
                await session.scalars(
                    select(WebSupportAttachment)
                    .where(WebSupportAttachment.message_id.in_(ids))
                    .order_by(WebSupportAttachment.id)
                )
            )
            if ids
            else []
        )
        grouped = {}
        for attachment in attachments:
            grouped.setdefault(attachment.message_id, []).append(
                attachment_json(attachment)
            )
        for message in messages:
            for media in ("photo", "document"):
                if getattr(message, media):
                    grouped.setdefault(message.id, []).append(
                        dict(
                            id=f"tg-{message.id}-{media}",
                            kind="image" if media == "photo" else "document",
                            name="Фото из Telegram"
                            if media == "photo"
                            else "Файл из Telegram",
                            url=f"/api/web/support/messages/{message.id}/media/{media}",
                        )
                    )
        from app.web.support_message_actions import web_actions
        result["messages"] = [
            {
                "id": message.id,
                **edit_meta.get(message.id,{}),
                "read_id":message.id,
                "delivery":("read" if reads["staff" if message.role=="client" else "client"]>=message.id else "saved") if message.role in ("client","admin") else None,
                "role": message.role,
                "text": message.text,
                "actions":web_actions(message.text,[]) if message.role=="admin" else [],
                "created_at": message.created_at.replace(
                    tzinfo=timezone.utc
                ).isoformat(),
                "channel": "site"
                if message.source.startswith("web:")
                else "telegram"
                if message.role != "system"
                else None,
                "attachments": grouped.get(message.id, []),
            }
            for message in messages
        ]
    if include_messages and conversation and settings:
        from app.services.freeman_ai.service import messages as history
        from app.web.ai_customer_chat import timeline
        _,rows=await timeline(session,settings,conversation,viewer,await history(session,conversation))
        result['messages']=[dict(id=m['id'],role='admin' if m['role'] in ('operator','assistant','specialist') else 'client' if m['role']=='client' else 'system',text=m['text'],created_at=m['created_at'],attachments=m.get('attachments',[]),read_id=m.get('read_id'),edit_id=m.get('edit_id'),edit_version=m.get('edit_version',0),edit_until=m.get('edit_until'),edited=m.get('edited',False),delivery=m.get('delivery'),actions=m.get('actions',[]),sender='agent' if m['role']=='assistant' else 'specialist' if m['role'] in ('operator','specialist') else None) for m in rows]
    return result
