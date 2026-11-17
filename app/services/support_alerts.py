"""Compact operator alerts; the website remains the sole support workspace."""
from datetime import timedelta
from html import escape
from sqlalchemy import select, func, or_
from app.database.models import AppSetting, ChatNotice, Ticket, TicketMessage, User, AIConversation
from app.database.repositories import SQLRepository
from app.services.vless_creation import utc

KIND = 'support_alert'
CURSOR = 'bot_support_alert_start'
COOLDOWN = timedelta(minutes=30)

async def human_needed(repo, settings, ticket):
    if not ticket or ticket.status == 'closed':
        return False
    from app.services.freeman_ai.customer_config import enabled
    if await enabled(repo.session, settings):
        ai = await repo.session.scalar(select(AIConversation.id).where(
            AIConversation.ticket_id == ticket.id, AIConversation.channel != 'owner', AIConversation.mode == 'ai').limit(1))
        if ai:
            return False
    return True

async def unanswered(repo, ticket, message_id):
    reply = await repo.session.scalar(select(func.max(TicketMessage.id)).where(
        TicketMessage.ticket_id == ticket.id, TicketMessage.role == 'admin',
        ~TicketMessage.source.like('auto:%')))
    return not reply or reply < message_id

async def queue(database, settings):
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        from app.services.support_group import TARGET, enqueue
        if await repo.setting(TARGET):
            return await enqueue(repo)
        destination = await repo.setting('history_chat_id')
        # Only the explicitly bound notifications group is used. No fallback DM.
        if not destination or not destination.startswith('-') or not destination[1:].isdigit():
            return 0
        cursor = await session.scalar(select(AppSetting).where(AppSetting.key == CURSOR).with_for_update())
        if cursor is None:
            latest = await session.scalar(select(func.max(TicketMessage.id))) or 0
            await repo.set_setting(CURSOR, str(latest))
            return 0  # Enable prospectively; never send the old support archive.
        # Keep a fixed activation boundary, not a moving high-water mark:
        # concurrent PostgreSQL transactions may commit message IDs out of order.
        latest = select(func.max(TicketMessage.id)).where(
            TicketMessage.ticket_id == Ticket.id, TicketMessage.role == 'client',
            TicketMessage.id > int(cursor.value)).correlate(Ticket).scalar_subquery()
        messages = list(await session.scalars(select(TicketMessage).join(Ticket, Ticket.id == TicketMessage.ticket_id).where(
            Ticket.status != 'closed', TicketMessage.id == latest)))
        clients = {m.ticket_id: m for m in messages}
        count = 0
        for tid, message in clients.items():
            ticket = await session.get(Ticket, tid)
            if not await human_needed(repo, settings, ticket) or not await unanswered(repo, ticket, message.id):
                continue
            previous = await session.scalar(select(ChatNotice).where(
                ChatNotice.kind == KIND, ChatNotice.ticket_id == tid,
                or_(ChatNotice.status == 'sent', ChatNotice.target_id == int(destination))
            ).order_by(ChatNotice.id.desc()).limit(1))
            if previous:
                if previous.status in ('pending', 'sending'):
                    continue
                anchor = max(utc(previous.created_at), utc(previous.available_at)) if previous.status == 'sent' else utc(previous.created_at)
                if utc(message.created_at) < anchor + COOLDOWN:
                    continue  # Only a NEW message after cooldown can alert again.
            await repo.add(ChatNotice(dedup=f'support.alert:{message.id}:{destination}', target_id=int(destination),
                kind=KIND, ticket_id=tid, text='', buttons='[]'))
            count += 1
        return count

async def screen(repo, settings, job):
    from app.services.support_group import TARGET
    if await repo.setting(TARGET):
        return None
    destination = await repo.setting('history_chat_id')
    if destination != str(job.target_id):
        return None
    ticket = await repo.session.get(Ticket, job.ticket_id)
    if not await human_needed(repo, settings, ticket):
        return None
    message_id = int(job.dedup.split(':')[1])
    if not await unanswered(repo, ticket, message_id):
        return None
    user = await repo.session.get(User, ticket.user_id)
    if not user:
        return None
    from app.services.accounts import display_freeman_id
    from app.services.admin_client_grant import rich
    identity = display_freeman_id(user.freeman_id) or f'FO-{user.id}'
    text = '💬 <b>Клиент написал в поддержку</b>\n\n' + escape(user.display_name[:120]) + ' · ' + escape(identity)
    if user.username:
        text += '\n@' + escape(user.username[:64])
    text += f'\nОбращение №{ticket.id}'
    # A plain authenticated website URL, never a bearer token in a group.
    target = 'url:' + settings.web_base_url.rstrip('/') + '/?support_ticket=' + str(ticket.id)
    result=rich(text, [[('💬 Открыть чат поддержки', target)]])
    result.button_options={target:{'style':'primary'}}
    return result


def formatted_excerpt(value, limit=1200):
    """Small, escaped Markdown subset for notification previews, never raw HTML."""
    import re
    value = (value or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    truncated = len(value) > limit
    value = value[:limit].rstrip()
    token = re.compile(r'`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|(?<!\w)\*[^*\n]+\*(?!\w)|(?<!\w)_[^_\n]+_(?!\w)|\[[^\]\n]+\]\(https?://[^\s)]+\)')
    def inline(line):
        parts=[];at=0
        for m in token.finditer(line):
            parts.append(escape(line[at:m.start()]));item=m.group();at=m.end()
            if item.startswith('`'):parts.append('<code>'+escape(item[1:-1])+'</code>')
            elif item.startswith(('**','__')):parts.append('<b>'+escape(item[2:-2])+'</b>')
            elif item.startswith(('*','_')):parts.append('<i>'+escape(item[1:-1])+'</i>')
            else:
                label,url=item[1:-1].split('](',1)
                parts.append('<a href="'+escape(url,quote=True)+'">'+escape(label)+'</a>')
        parts.append(escape(line[at:]));return ''.join(parts)
    lines=[]
    for line in value.splitlines():
        heading=re.match(r'^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+)?$',line)
        if heading:lines.append('<b>'+inline(heading.group(1))+'</b>')
        else:lines.append(inline(re.sub(r'^\s*[-*+]\s+', '• ',line)))
    result=re.sub(r'\n{3,}','\n\n','\n'.join(lines))
    if truncated:result+='\n\nПолный ответ — в чате.'
    return result

async def client_screen(repo, settings, job):
    """One compact reply notice per ticket/30 minutes, bound to its customer."""
    ticket=await repo.session.get(Ticket,job.ticket_id)
    customer=await repo.session.get(User,ticket.user_id) if ticket else None
    if not customer or customer.telegram_id!=job.target_id or job.target_id<=0 or customer.status!='active':
        return None
    if job.dedup.startswith('ticket.auto:'):
        return None  # Acknowledgements must not mask or multiply human replies.
    text=job.text
    is_reply=job.dedup.startswith('ticket.message:')
    if is_reply:
        reference=await repo.session.get(TicketMessage,int(job.dedup.split(':')[1]))
        if not reference or reference.ticket_id!=ticket.id or reference.role!='admin' or reference.source.startswith('auto:'):
            return None
        reply=await repo.session.scalar(select(TicketMessage).where(TicketMessage.ticket_id==ticket.id,
            TicketMessage.role=='admin',~TicketMessage.source.like('auto:%'),TicketMessage.id>=reference.id).order_by(TicketMessage.id.desc()).limit(1))
        last_client=await repo.session.scalar(select(func.max(TicketMessage.id)).where(TicketMessage.ticket_id==ticket.id,TicketMessage.role=='client'))
        read=await repo.setting(f'support_read:{customer.id}:{ticket.id}','0')
        if (last_client and last_client>reply.id) or (read.isdigit() and int(read)>=reply.id):
            return None
        previous=await repo.session.scalar(select(ChatNotice).where(ChatNotice.kind=='client',ChatNotice.ticket_id==ticket.id,
            ChatNotice.target_id==job.target_id,ChatNotice.status=='sent',ChatNotice.dedup.like('ticket.message:%')).order_by(ChatNotice.id.desc()).limit(1))
        if previous and utc(reply.created_at)<max(utc(previous.created_at),utc(previous.available_at))+COOLDOWN:
            return None
        text=reply.text
    from app.services.web_login import issue_direct
    from app.services.admin_client_grant import rich
    from urllib.parse import urlencode
    token=issue_direct(settings,customer,purpose=f'direct_chat:{ticket.id}')
    target='webapp:'+settings.web_base_url.rstrip('/')+'/auth/bot/chat?'+urlencode(dict(ticket=ticket.id,token=token))
    heading='💬 <b>Поддержка ответила</b>' if is_reply else '💬 <b>Обновление обращения</b>'
    body=formatted_excerpt(text) or 'Поддержка отправила вложение. Посмотрите его в чате.'
    result=rich(heading+f'\nОбращение №{ticket.id}\n\n'+body,[[('💬 Открыть чат',target)]])
    result.button_options={target:{'style':'primary'}}
    return result
