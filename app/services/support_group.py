"""Private Telegram notifications for the support inbox."""
import json
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram.types import InputRichBlockButtons, RichMessageButton
from sqlalchemy import select

from app.database.models import AIConversation, AIEvent, AIMessage, ChatNotice, Ticket, TicketMessage, User, now
from app.services.admin_client_grant import rich
from app.services.freeman_ai.security import aware, redact

TARGET = 'support_group_chat_id'
SINCE = 'support_group_since'
KIND = 'support_group'
DELAY = timedelta(minutes=10)
TZ = ZoneInfo('Europe/Moscow')
SUPPORT_CHANNELS = ('website', 'telegram', 'support_agent_website')
TITLES = {'first': '💬 Новый диалог поддержки',
          'handoff': '👤 Агент передал диалог специалисту',
          'overdue': '🚨 Клиент ждёт ответа специалиста'}


async def bind(repo, chat_id):
    if chat_id >= 0 or str(chat_id) in (
        await repo.setting('history_chat_id'), await repo.setting('infra_chat_id'),
        await repo.setting('support_chat_id'),
    ):
        raise ValueError('Отправьте команду в отдельной группе уведомлений поддержки.')
    if await repo.setting(TARGET) != str(chat_id):
        await repo.set_setting(TARGET, str(chat_id))
        await repo.set_setting(SINCE, now().isoformat())


def real_client_messages(ticket_id):
    return select(TicketMessage).where(
        TicketMessage.ticket_id == ticket_id, TicketMessage.role == 'client',
        ~TicketMessage.source.startswith('ai:handoff:'))


async def latest_handoff(session, conversation_id):
    return await session.scalar(select(AIEvent).where(
        AIEvent.conversation_id == conversation_id, AIEvent.kind == 'handoff',
        AIEvent.name.in_(('waiting_operator', 'human'))).order_by(AIEvent.id.desc()).limit(1))


async def human_reply(session, ticket, conv, since):
    if ticket:
        reply = await session.scalar(select(TicketMessage.id).where(
            TicketMessage.ticket_id == ticket.id, TicketMessage.role == 'admin',
            ~TicketMessage.source.startswith('ai:'), ~TicketMessage.source.startswith('auto:'),
            TicketMessage.created_at >= since).limit(1))
        if reply:
            return True
    if conv:
        return bool(await session.scalar(select(AIMessage.id).where(
            AIMessage.conversation_id == conv.id, AIMessage.role == 'operator',
            AIMessage.created_at >= since).limit(1)))
    return False


async def waiting_for_operator(repo, conv, ticket):
    if ticket and ticket.status == 'closed':
        return False
    if conv:
        return (conv.mode == 'human' or (conv.mode == 'ai' and
                await repo.setting(f'agent_v21_waiting:{conv.id}') == '1'))
    return bool(ticket and ticket.status == 'waiting_admin')


def notification_allowed(stage, conv, ticket):
    if conv and conv.channel not in SUPPORT_CHANNELS:
        return False
    if ticket and ticket.status == 'closed':
        return False
    # The assistant's opening turn is not an explicit support request.
    return bool(ticket) if stage == 'first' else bool(ticket or conv)


def conversation_link(settings, ticket, conv, source=''):
    base = settings.web_base_url.rstrip('/')
    telegram = (source.startswith('tg:') or source.partition(':')[0].isdigit()
                or bool(conv and conv.channel == 'telegram' and not source.startswith('web:')))
    if ticket and telegram and settings.bot_username:
        return 'Telegram · бот поддержки', f'https://t.me/{settings.bot_username}?start=ops_ticket_{ticket.id}'
    if ticket:
        return 'Сайт · поддержка', f'{base}/?support_ticket={ticket.id}'
    channel = 'Telegram · бот поддержки' if telegram else 'Сайт · передано специалисту'
    return channel, f'{base}/admin/ai?chat={conv.id}'


async def enqueue(repo):
    target = await repo.setting(TARGET)
    after = await repo.setting(SINCE)
    if not target or not after:
        return 0
    cutoff = max(datetime.fromisoformat(after), now() - timedelta(days=1))
    session = repo.session
    open_cases = list(await session.scalars(select(Ticket).where(
        Ticket.status != 'closed', Ticket.updated_at >= cutoff)))
    conversations = list(await session.scalars(select(AIConversation).where(
        AIConversation.mode.in_(('ai', 'human')),
        (AIConversation.ticket_id.in_([ticket.id for ticket in open_cases]))
        | (AIConversation.ticket_id.is_(None) & AIConversation.channel.in_(SUPPORT_CHANNELS)
           & (AIConversation.updated_at >= cutoff)),
    ).order_by(AIConversation.id)))
    attached = {conv.ticket_id: conv for conv in conversations if conv.ticket_id}
    cases = [(ticket, attached.get(ticket.id)) for ticket in open_cases]
    cases += [(None, conv) for conv in conversations if not conv.ticket_id]
    pending = {}
    for ticket, conv in cases:
        if not notification_allowed('handoff', conv, ticket):
            continue
        if not ticket and not await waiting_for_operator(repo, conv, ticket):
            continue
        if ticket:
            query = real_client_messages(ticket.id)
            first = await session.scalar(query.order_by(TicketMessage.id).limit(1))
            last = await session.scalar(query.order_by(TicketMessage.id.desc()).limit(1))
        else:
            query = select(AIMessage).where(AIMessage.conversation_id == conv.id, AIMessage.role == 'client')
            first = await session.scalar(query.order_by(AIMessage.id).limit(1))
            last = await session.scalar(query.order_by(AIMessage.id.desc()).limit(1))
        subject = f'a:{conv.id}' if conv else f't:{ticket.id}'
        if notification_allowed('first', conv, ticket) and first and aware(first.created_at) >= cutoff:
            pending[f'support.group:first:{subject}:0'] = ticket.id if ticket else None
        if not await waiting_for_operator(repo, conv, ticket):
            continue
        handoff = await latest_handoff(session, conv.id) if conv else None
        if handoff and aware(handoff.created_at) >= cutoff:
            reason = json.loads(handoff.details or '{}').get('reason')
            if reason not in ('operator_takeover', 'operator_reply'):
                pending[f'support.group:handoff:{subject}:{handoff.id}'] = ticket.id if ticket else None
        else:
            handoff = None
        if handoff:
            anchor = handoff.created_at
            version = f'a{handoff.id}'
        elif last and aware(last.created_at) >= cutoff:
            # Start at the first unanswered message so extra messages don't reset the timer.
            if not ticket:
                continue
            reply_at = await session.scalar(select(TicketMessage.created_at).where(
                TicketMessage.ticket_id == ticket.id, TicketMessage.role == 'admin',
                ~TicketMessage.source.startswith('ai:'), ~TicketMessage.source.startswith('auto:')
            ).order_by(TicketMessage.id.desc()).limit(1))
            oldest = real_client_messages(ticket.id).where(TicketMessage.created_at >= cutoff)
            if reply_at:
                oldest = oldest.where(TicketMessage.created_at > reply_at)
            next_message = await session.scalar(oldest.order_by(TicketMessage.id).limit(1))
            if not next_message:
                continue
            anchor, version = next_message.created_at, f't{next_message.id}'
        else:
            continue
        if aware(anchor) <= now() - DELAY and not await human_reply(session, ticket, conv, anchor):
            pending[f'support.group:overdue:{subject}:{version}'] = ticket.id if ticket else None
    if not pending:
        return 0
    existing = set(await session.scalars(select(ChatNotice.dedup).where(ChatNotice.dedup.in_(pending))))
    for key in (key for key in pending if key not in existing):
        await repo.add(ChatNotice(dedup=key, kind=KIND, ticket_id=pending[key], target_id=int(target), text=''))
    return len(pending.keys() - existing)


def excerpt(value):
    return escape(redact(value).strip()[:850] or '📎 Вложение')


def timestamp(value):
    return aware(value).astimezone(TZ).strftime('%H:%M')


def card(stage, client, incoming, previous, actions):
    body = f'<b>{TITLES[stage]}</b>\n\n{escape(client)}'
    if previous:
        label, stamp, value = previous
        body += f'\n\n<b>{label} · {timestamp(stamp)}</b>\n<blockquote>{excerpt(value)}</blockquote>'
    if incoming:
        stamp, value = incoming
        body += f'\n\n<b>Клиент · {timestamp(stamp)} МСК</b>\n<blockquote>{excerpt(value)}</blockquote>'
    screen = rich(body, [])
    for label, kind, target in actions:
        button = RichMessageButton(text=label, style='primary', **{kind: target})
        screen.rich.blocks.append(InputRichBlockButtons(buttons=[button]))
        if kind == 'url':
            screen.text += f'\n<a href="{escape(target, quote=True)}">{escape(label)}</a>'
    return screen


async def screen_for(repo, settings, job):
    if await repo.setting(TARGET) != str(job.target_id):
        return None
    _, stage, domain, ident, version = job.dedup.split(':')
    session = repo.session
    conv = await session.get(AIConversation, int(ident)) if domain == 'a' else None
    ticket_id = conv.ticket_id if conv else int(ident) if domain == 't' else None
    ticket = await session.get(Ticket, ticket_id) if ticket_id else None
    if ticket and not conv:
        conv = await session.scalar(select(AIConversation).where(
            AIConversation.ticket_id == ticket.id, AIConversation.mode.in_(('ai', 'human'))
        ).order_by(AIConversation.id.desc()).limit(1))
    if not notification_allowed(stage, conv, ticket):
        return None
    if stage != 'first' and not await waiting_for_operator(repo, conv, ticket):
        return None
    if stage == 'overdue':
        model = AIEvent if version.startswith('a') else TicketMessage
        anchor = await session.get(model, int(version[1:]))
        if not anchor or await human_reply(session, ticket, conv, anchor.created_at):
            return None
    elif stage == 'handoff':
        anchor = await session.get(AIEvent, int(version))
        if not anchor or await human_reply(session, ticket, conv, anchor.created_at):
            return None
    client = await session.get(User, ticket.user_id if ticket else conv.user_id) if (ticket or conv.user_id) else None
    name = client.display_name if client else 'Посетитель'
    if client and client.freeman_id:
        name += f' · {client.freeman_id}'
    if ticket:
        name += f' · обращение №{ticket.id}'
    incoming = previous = None
    if ticket:
        order = TicketMessage.id if stage == 'first' else TicketMessage.id.desc()
        current = await session.scalar(real_client_messages(ticket.id).order_by(order).limit(1))
        if current:
            prev = await session.scalar(select(TicketMessage).where(
                TicketMessage.ticket_id == ticket.id, TicketMessage.role == 'admin',
                TicketMessage.id < current.id, ~TicketMessage.source.startswith('auto:')
            ).order_by(TicketMessage.id.desc()).limit(1))
            if prev:
                previous=('Агент' if prev.source.startswith('ai:') else 'Специалист',prev.created_at,prev.text)
    else:
        current = None
    if not current and conv:
        order = AIMessage.id if stage == 'first' else AIMessage.id.desc()
        current = await session.scalar(select(AIMessage).where(
            AIMessage.conversation_id == conv.id, AIMessage.role == 'client').order_by(order).limit(1))
        if current:
            prev = await session.scalar(select(AIMessage).where(
                AIMessage.conversation_id == conv.id, AIMessage.role.in_(('operator', 'assistant')),
                AIMessage.id < current.id).order_by(AIMessage.id.desc()).limit(1))
            if prev:
                previous=('Специалист' if prev.role == 'operator' else 'Агент',prev.created_at,prev.text)
    if current:
        incoming=(current.created_at,current.text)
    channel, url = conversation_link(settings, ticket, conv, getattr(current, 'source', ''))
    name += '\n' + channel
    actions = [('Открыть диалог', 'url', url)]
    if client and settings.bot_username:
        actions.append(('Карточка клиента','url',f'https://t.me/{settings.bot_username}?start=ops_client_{client.id}'))
    if stage != 'first' and conv and conv.channel in ('website','telegram'):
        actions.append(('Передать агенту','callback_data',f'support_group:resume:{conv.id}'))
    return card(stage,name,incoming,previous,actions)


async def resume(repo, settings, actor, chat_id, action):
    from app.services.freeman_ai import service

    if not settings.can_admin_action(actor.telegram_id, 'admin:t:home') or str(chat_id) != await repo.setting(TARGET):
        raise PermissionError('Недоступно в этом чате.')
    ident = action.removeprefix('support_group:resume:')
    if not ident.isascii() or not ident.isdecimal() or not 0 < int(ident) < 2**63:
        raise ValueError('Диалог недоступен.')
    conv = await repo.session.get(AIConversation, int(ident))
    if not conv or conv.channel not in ('website','telegram') or conv.mode in ('closed','deleted'):
        raise ValueError('Диалог недоступен.')
    await service.lock_ticket(repo.session, conv)
    await repo.session.refresh(conv, with_for_update=True)
    ticket = await repo.session.get(Ticket, conv.ticket_id) if conv.ticket_id else None
    if not await waiting_for_operator(repo, conv, ticket):
        raise ValueError('Диалог уже у агента.')
    if not await service.channel_enabled(repo.session, settings, conv.channel):
        raise ValueError('Агент сейчас недоступен.')
    await service.switch_mode(repo.session, settings, conv, 'ai')
