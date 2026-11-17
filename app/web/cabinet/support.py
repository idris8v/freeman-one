"""Customer and operator support conversations and ticket lifecycle."""

import hashlib
import json
import secrets
from pathlib import Path

from app.web.request_performance import measured_route
from fastapi import APIRouter, Cookie, HTTPException, Request
from sqlalchemy import func, select, cast, String

from app.database.models import (
    AppSetting,
    ChatNotice,
    Ticket,
    TicketMessage,
    User,
    WebQuickReply,
    WebSupportAttachment,
)
from app.database.repositories import SQLRepository
from app.web.cabinet.context import SUPPORT_STORAGE, WebContext
from app.web.cabinet.schemas import (
    SupportFlagsPayload,
    SupportMessagePayload,
    SupportStatusPayload,
)
from app.web.cabinet.support_presenter import attachment_json, ticket_json as present_ticket
from app.web.cabinet.support_storage import safe_attachment, store_attachments


def support_router(context: WebContext) -> APIRouter:
    database = context.database
    settings = context.settings
    current_user = context.current_user
    router = APIRouter(route_class=measured_route(database.engine))
    from app.web.support_message_edit import edit_router
    router.routes.extend(edit_router(context).routes)
    from app.web.support_receipts import receipts_router
    router.routes.extend(receipts_router(context).routes)
    from app.web.support_materials import materials_router
    router.routes.extend(materials_router(context).routes)
    async def ticket_json(session,item,viewer,is_admin=False,include_messages=False):
        return await present_ticket(session,item,viewer,is_admin,include_messages,settings=settings)

    from app.web.assistant_voice import Dictation
    dictation = Dictation()

    @router.post("/api/web/support/dictation")
    async def support_dictation(request: Request, freeman_session: str | None = Cookie(default=None)):
        user = await current_user(freeman_session)
        if user.status == "blocked":
            raise HTTPException(403, "Аккаунт заблокирован.")
        return await dictation.response(request, settings, user.id)


    @router.get("/api/web/support")
    async def support(
        ticket_id: int | None = None,
        client: bool = False,
        status: str = "all",
        q: str = "",
        page: int = 1,
        limit: int = 10,
        scope: str = "operator",
        freeman_session: str | None = Cookie(default=None),
    ):
        user = await current_user(freeman_session)
        is_admin = (
            settings.can_admin_action(user.telegram_id, "admin:t:home") and not client
        )
        from app.services.tickets import STATUSES

        if (
            scope not in ("operator","all")
            or status not in ("all", "open", "important", *STATUSES)
            or len(q) > 100
            or page < 1
            or limit not in (10, 25, 50, 100)
        ):
            raise HTTPException(422, "Недопустимый фильтр")
        async with database.sessions.begin() as session:
            repo = SQLRepository(session)
            routing_filters = []
            filters = routing_filters
            if is_admin and scope=='operator':
                from app.services.freeman_ai.customer_config import enabled as customer_enabled
                from app.database.models import AIConversation
                if await customer_enabled(session,settings):
                    filters.append(~select(AIConversation.id).where(AIConversation.ticket_id==Ticket.id,AIConversation.channel!='owner',AIConversation.mode=='ai',~select(AppSetting.key).where(AppSetting.key=='agent_v21_waiting:'+cast(AIConversation.id,String),AppSetting.value=='1').exists()).exists())

            filters=list(routing_filters)
            if not is_admin:
                filters.append(Ticket.user_id == user.id)
            if status == "important":
                if not is_admin:
                    raise HTTPException(403, "Нет доступа")
                filters.append(select(AppSetting.key).where(AppSetting.key == "support_important:" + cast(Ticket.id, String), AppSetting.value == "1").exists())
            elif status == "open":
                filters.append(Ticket.status != "closed")
            elif status != "all":
                filters.append(Ticket.status == status)
            if q.strip():
                from sqlalchemy import or_

                term = "%" + q.strip().replace("%", "\\%").replace("_", "\\_") + "%"
                matches = select(TicketMessage.ticket_id).where(
                    TicketMessage.text.ilike(term, escape="\\")
                )
                people = select(User.id).where(
                    or_(
                        User.first_name.ilike(term, escape="\\"),
                        User.freeman_id.ilike(term, escape="\\"),
                        User.username.ilike(term, escape="\\"),
                    )
                )
                filters.append(
                    or_(
                        Ticket.id.in_(matches),
                        Ticket.user_id.in_(people),
                        Ticket.id == int(q) if q.isdigit() else False,
                    )
                )
            if is_admin:
                ranked = (
                    select(
                        Ticket.id.label("id"),
                        func.row_number()
                        .over(
                            partition_by=Ticket.user_id,
                            order_by=(Ticket.updated_at.desc(), Ticket.id.desc()),
                        )
                        .label("rank"),
                    )
                    .where(*filters)
                    .subquery()
                )
                filters = [Ticket.id.in_(select(ranked.c.id).where(ranked.c.rank == 1))]
            count = await session.scalar(
                select(func.count()).select_from(Ticket).where(*filters)
            )
            page = min(page, max(1, (count + limit - 1) // limit))
            from sqlalchemy import case

            pinned_keys = (
                list(
                    await session.scalars(
                        select(AppSetting.key).where(
                            AppSetting.key.like("support_pinned:%"),
                            AppSetting.value == "1",
                        )
                    )
                )
                if is_admin
                else []
            )
            pinned_ids = [
                int(key.split(":")[-1])
                for key in pinned_keys
                if key.split(":")[-1].isdigit()
            ]
            tickets = list(
                await session.scalars(
                    select(Ticket)
                    .where(*filters)
                    .order_by(
                        case((Ticket.id.in_(pinned_ids), 0), else_=1),
                        Ticket.updated_at.desc(),
                        Ticket.id.desc(),
                    )
                    .offset((page - 1) * limit)
                    .limit(limit)
                )
            )
            selected = await session.get(Ticket, ticket_id) if ticket_id else None
            if ticket_id and (
                not selected or not is_admin and selected.user_id != user.id
            ):
                raise HTTPException(404, "Обращение недоступно")
            from app.web.cabinet.support_library import customer_ids
            help_ids = await customer_ids(session)
            help_ready = await repo.setting("web_customer_help_initialized", "0") == "1"
            quick_help = [] if help_ready or help_ids else None
            replies = []
            material_ids={await repo.setting('support_guide_20261108:replies:'+str(i)) for i in (0,4)}
            for reply in await session.scalars(
                select(WebQuickReply)
                .where(WebQuickReply.enabled == True)
                .order_by(WebQuickReply.position, WebQuickReply.id)
            ):
                if not is_admin and reply.id not in help_ids:
                    continue
                if is_admin and reply.id in help_ids:
                    continue
                assets = await session.scalars(
                    select(WebSupportAttachment).where(
                        WebSupportAttachment.quick_reply_id == reply.id
                    )
                )
                (replies if is_admin else quick_help).append(
                    dict(
                        id=reply.id,
                        title=reply.title,
                        material='appstore-region' if str(reply.id) in material_ids else None,
                        text=reply.text,
                        attachments=[attachment_json(x) for x in assets],
                    )
                )
            counts = {}
            for value in STATUSES:
                counts[value] = await session.scalar(
                    select(func.count())
                    .select_from(Ticket)
                    .where(
                        Ticket.status == value,
                        *(routing_filters if is_admin else [Ticket.user_id==user.id]),
                    )
                )
            return dict(
                is_admin=is_admin,
                tickets=[
                    await ticket_json(session, t, user, is_admin) for t in tickets
                ],
                selected=await ticket_json(session, selected, user, is_admin, True)
                if selected
                else None,
                quick_replies=replies,
                quick_help=quick_help,
                counts=counts,
                total=count,
                page=page,
                limit=limit,
                autoreply=dict(
                    enabled=await repo.setting("web_support_autoreply_enabled", "0")
                    == "1",
                    text=await repo.setting(
                        "web_support_autoreply_text",
                        "Спасибо! Мы получили сообщение и скоро ответим.",
                    ),
                ),
            )

    @router.post("/api/web/support/messages")
    async def support_message(
        payload: SupportMessagePayload,
        freeman_session: str | None = Cookie(default=None),
    ):
        user = await current_user(freeman_session)
        is_admin = (
            settings.can_admin_action(user.telegram_id, "admin:t:home")
            and not payload.client_mode
        )
        from app.services.tickets import STATUSES

        if payload.response_status not in STATUSES:
            raise HTTPException(422, "Неизвестный статус ответа")
        if (
            not payload.text.strip()
            and not payload.attachments
            and not payload.quick_reply_id
        ):
            raise HTTPException(422, "Введите сообщение или приложите фото/видео")
        async with database.sessions.begin() as session:
            repo = SQLRepository(session)
            text = payload.text.strip()
            quick_assets = []
            if payload.quick_reply_id and not is_admin:
                raise HTTPException(403, "Быстрые ответы доступны только оператору")
            if is_admin and payload.quick_reply_id:
                quick = await session.get(WebQuickReply, payload.quick_reply_id)
                from app.web.cabinet.support_library import customer_ids
                if not quick or not quick.enabled or quick.id in await customer_ids(session):
                    raise HTTPException(404, "Быстрый ответ не найден")
                text = payload.text.strip() or quick.text
                quick_assets = list(
                    await session.scalars(
                        select(WebSupportAttachment).where(
                            WebSupportAttachment.quick_reply_id == quick.id
                        )
                    )
                )
            if any(asset.kind == 'audio' for asset in quick_assets):
                raise HTTPException(422, 'Голосовые сообщения отключены. Используйте голосовой ввод в текст.')
            for asset in payload.attachments:
                safe_attachment(asset)
            if (
                sum(len(asset.data) * 3 // 4 for asset in payload.attachments)
                > 40 * 1024 * 1024
            ):
                raise HTTPException(422, "Общий размер вложений — до 40 МБ")
            ai_target = None
            if is_admin and payload.ticket_id:
                from app.database.models import AIConversation
                linked = await session.scalar(select(AIConversation).where(AIConversation.ticket_id==payload.ticket_id, AIConversation.mode.not_in(('closed','deleted'))).order_by(AIConversation.id.desc()).limit(1))
                if linked and linked.channel=='telegram':
                    from app.web.ai_customer_chat import telegram_target
                    ai_target = await telegram_target(session,settings,linked)
                    if not ai_target:raise HTTPException(409,'Telegram-получатель диалога не подтверждён.')
                elif linked and linked.channel not in ('website','owner') and (payload.attachments or quick_assets):
                    raise HTTPException(409,'Вложения для этого канала не поддерживаются.')
            source = f"web:{user.id}:" + (payload.request_id or secrets.token_hex(24))
            fingerprint = hashlib.sha256(
                json.dumps(
                    payload.model_dump(exclude={"request_id"}), sort_keys=True
                ).encode()
            ).hexdigest()
            existing = await session.scalar(
                select(TicketMessage).where(TicketMessage.source == source)
            )
            if existing:
                if await repo.setting("support_request:" + source) != fingerprint:
                    raise HTTPException(
                        409, "Идентификатор уже использован для другого сообщения"
                    )
                existing_ticket = await session.get(Ticket, existing.ticket_id)
                if not is_admin and existing_ticket.user_id != user.id:
                    raise HTTPException(404, "Обращение недоступно")
                return await ticket_json(session, existing_ticket, user, is_admin, True)
            from app.services.tickets import TicketService

            service = TicketService(repo, settings)
            try:
                action = (
                    f"admin:t:input:reply:{payload.ticket_id}"
                    if is_admin
                    else (
                        f"t:input:reply:{payload.ticket_id}"
                        if payload.ticket_id
                        else "t:input:new"
                    )
                )
                notice = text or (
                    "📎 Поддержка отправила вложение."
                    if is_admin
                    else "📎 Клиент приложил файл."
                )
                await service.input(
                    action,
                    user,
                    notice,
                    source,
                    force_new=not is_admin and payload.ticket_id is None,
                )
            except (ValueError, PermissionError) as error:
                raise HTTPException(409, str(error)) from error
            message = await session.scalar(
                select(TicketMessage).where(TicketMessage.source == source)
            )
            stored = await repo.setting("support_request:" + source)
            if stored:
                if stored != fingerprint:
                    raise HTTPException(
                        409, "Идентификатор уже использован для другого сообщения"
                    )
                return await ticket_json(
                    session,
                    await session.get(Ticket, message.ticket_id),
                    user,
                    is_admin,
                    True,
                )
            if ai_target:
                # TicketService mirrors text into AIOutbox. Route this support reply
                # through the existing support queue atomically, including its media.
                from app.database.models import AIMessage, AIOutbox
                from app.services.freeman_ai.security import fingerprint as ai_fingerprint
                from sqlalchemy import update
                mirrored=await session.scalar(select(AIMessage.id).where(AIMessage.conversation_id==linked.id,AIMessage.source_hash==ai_fingerprint(settings,'message:telegram:ticket:'+str(message.id))))
                if mirrored:
                    await session.execute(update(AIOutbox).where(AIOutbox.message_id==mirrored,AIOutbox.status=='pending').values(status='canceled',error_code='support_queue'))
                await repo.add(ChatNotice(dedup=f'ticket.message:{message.id}:{ai_target}',target_id=ai_target,kind='client',ticket_id=message.ticket_id,text=text or 'Поддержка отправила вложение.',buttons='[]'))
            await repo.set_setting("support_request:" + source, fingerprint)
            saved = await store_attachments(
                session, payload.attachments, message_id=message.id
            )
            if quick_assets:
                SUPPORT_STORAGE.mkdir(parents=True, exist_ok=True)
                for asset in quick_assets:
                    from app.web.cabinet.support_storage import reference_asset
                    clone = await reference_asset(session, asset, message.id)
                    saved.append(clone)
            ticket = await session.get(Ticket, message.ticket_id)
            notices = list(
                await session.scalars(
                    select(ChatNotice).where(
                        ChatNotice.dedup.like(f"ticket.message:{message.id}:%")
                    )
                )
            )
            # Prospectively replace the ordinary website notice with one rich reply.
            if is_admin and payload.quick_reply_id and quick_assets and saved:
                for notice in notices:
                    if notice.kind == 'client':
                        notice.kind = 'support_media_reply'
            for asset in saved:
                for notice in notices:
                    if notice.kind == 'support_media_reply':
                        continue
                    as_photo = (
                        asset.mime_type in ("image/jpeg", "image/png")
                        and asset.size <= 10 * 1024 * 1024
                    )
                    await repo.add(
                        ChatNotice(
                            dedup=f"ticket.asset:{asset.id}:{notice.target_id}",
                            target_id=notice.target_id,
                            kind=notice.kind,
                            ticket_id=ticket.id,
                            text=f"Вложение к обращению #{ticket.id}",
                            photo=f"web-support:{asset.id}" if as_photo else None,
                            document=f"web-support:{asset.id}"
                            if not as_photo
                            else None,
                            buttons="[]",
                        )
                    )
            if is_admin and payload.response_status != "waiting_client":
                await service.set_status(ticket.id, user, payload.response_status, True)
            return await ticket_json(session, ticket, user, is_admin, True)

    @router.delete("/api/web/support/tickets/{ticket_id}")
    async def delete_support_ticket(
        ticket_id: int,
        request: Request,
        freeman_session: str | None = Cookie(default=None),
    ):
        if request.headers.get("sec-fetch-site") == "cross-site" or request.headers.get(
            "origin"
        ) not in (None, settings.web_base_url.rstrip("/")):
            raise HTTPException(403, "Недопустимый источник запроса")
        user = await current_user(freeman_session)
        async with database.sessions.begin() as session:
            from app.services.tickets import TicketService

            try:
                await TicketService(SQLRepository(session), settings).delete(
                    ticket_id, user
                )
            except PermissionError as e:
                raise HTTPException(403, str(e))
            except ValueError as e:
                raise HTTPException(404, str(e))
        return {"deleted": True}

    @router.post("/api/web/support/tickets/{ticket_id}/close")
    async def close_support_ticket(
        ticket_id: int,
        client: bool = False,
        freeman_session: str | None = Cookie(default=None),
    ):
        user = await current_user(freeman_session)
        is_admin = (
            settings.can_admin_action(user.telegram_id, "admin:t:home") and not client
        )
        async with database.sessions.begin() as session:
            ticket = await session.get(Ticket, ticket_id)
            if not ticket or (not is_admin and ticket.user_id != user.id):
                raise HTTPException(404, "Обращение не найдено")
            from app.services.tickets import TicketService

            try:
                await TicketService(SQLRepository(session), settings).set_status(
                    ticket_id, user, "closed", is_admin
                )
            except PermissionError as e:
                raise HTTPException(403, str(e))
            return {"id": ticket.id, "status": ticket.status}

    @router.post("/api/web/support/tickets/{ticket_id}/status")
    async def support_status(
        ticket_id: int,
        payload: SupportStatusPayload,
        freeman_session: str | None = Cookie(default=None),
    ):
        user = await current_user(freeman_session)
        admin = settings.can_admin_action(user.telegram_id, "admin:t:home")
        async with database.sessions.begin() as session:
            from app.services.tickets import TicketService

            try:
                item = await TicketService(SQLRepository(session), settings).set_status(
                    ticket_id, user, payload.status, admin
                )
            except PermissionError as e:
                raise HTTPException(403, str(e))
            except ValueError as e:
                raise HTTPException(404, str(e))
            return await ticket_json(session, item, user, admin, True)

    @router.post("/api/web/support/tickets/{ticket_id}/flags")
    async def support_flags(
        ticket_id: int,
        payload: SupportFlagsPayload,
        freeman_session: str | None = Cookie(default=None),
    ):
        actor = await current_user(freeman_session)
        if not settings.can_admin_action(actor.telegram_id, "admin:t:home"):
            raise HTTPException(403, "Нет доступа")
        async with database.sessions.begin() as session:
            if not await session.get(Ticket, ticket_id):
                raise HTTPException(404, "Обращение не найдено")
            repo = SQLRepository(session)
            for field in ("pinned", "important"):
                value = getattr(payload, field)
                if value is not None:
                    await repo.set_setting(
                        f"support_{field}:{ticket_id}", "1" if value else "0"
                    )
            return {"ok": True}

    return router
