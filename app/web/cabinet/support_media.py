"""Authorized download of support attachments from local storage or Telegram."""

import mimetypes

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import FileResponse, Response

from app.database.models import Ticket, TicketMessage, WebSupportAttachment
from app.web.cabinet.context import SUPPORT_STORAGE, WebContext


def support_media_router(context: WebContext) -> APIRouter:
    database = context.database
    settings = context.settings
    current_user = context.current_user
    router = APIRouter()

    @router.get("/api/web/support/messages/{message_id}/media/{kind}")
    async def telegram_support_media(
        message_id: int, kind: str, preview: str = "", freeman_session: str | None = Cookie(default=None)
    ):
        user = await current_user(freeman_session)
        if kind not in ("photo", "document"):
            raise HTTPException(404, "Файл недоступен")
        async with database.sessions() as session:
            message = await session.get(TicketMessage, message_id)
            ticket = await session.get(Ticket, message.ticket_id) if message else None
            if (
                not ticket
                or not settings.can_admin_action(user.telegram_id, "admin:t:home")
                and ticket.user_id != user.id
            ):
                raise HTTPException(404, "Файл недоступен")
            file_id = getattr(message, kind)
            if not file_id:
                raise HTTPException(404, "Файл отсутствует")
        from app.web.chat_photos import _CACHE, preview_bytes, _variant
        cache_key=('telegram', message_id, file_id)
        if preview and kind=='photo':
            cached=_CACHE.get((cache_key,_variant(preview)))
            if cached is not None:
                return Response(cached,media_type='image/webp',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
        from aiogram import Bot

        try:
            async with Bot(settings.bot_token) as bot:
                meta = await bot.get_file(file_id)
                if (meta.file_size or 0) > 20 * 1024 * 1024:
                    raise HTTPException(413, "Файл больше 20 МБ")
                buffer = await bot.download_file(meta.file_path)
            mime = (
                "image/jpeg"
                if kind == "photo"
                else mimetypes.guess_type(meta.file_path)[0]
                or "application/octet-stream"
            )
            from app.web.security import telegram_media_headers

            if preview and kind=='photo':
                data=await preview_bytes(buffer.getvalue(),cache_key,preview)
                return Response(data,media_type='image/webp',headers=telegram_media_headers(kind,meta.file_path))
            return Response(
                buffer.getvalue(),
                media_type=mime,
                headers=telegram_media_headers(kind, meta.file_path),
            )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                502, "Не удалось загрузить файл из Telegram. Попробуйте ещё раз."
            )

    @router.get("/api/web/support/attachments/{attachment_id}")
    async def support_attachment(
        attachment_id: int, preview: str = "", freeman_session: str | None = Cookie(default=None)
    ):
        user = await current_user(freeman_session)
        is_admin = settings.can_admin_action(user.telegram_id, "admin:t:home")
        async with database.sessions() as session:
            attachment = await session.get(WebSupportAttachment, attachment_id)
            if not attachment:
                raise HTTPException(404, "Вложение не найдено")
            if attachment.quick_reply_id:
                from app.web.cabinet.support_library import customer_ids
                from app.database.models import WebQuickReply
                template = await session.get(WebQuickReply, attachment.quick_reply_id)
                published_help = template and template.enabled and template.id in await customer_ids(session)
                if not settings.is_owner(user.telegram_id) and not published_help:
                    raise HTTPException(404, "Файл недоступен")
            else:
                message = await session.get(TicketMessage, attachment.message_id)
                ticket = await session.get(Ticket, message.ticket_id) if message else None
                if not ticket:
                    raise HTTPException(404, "Обращение отсутствует")
                if not is_admin and ticket.user_id != user.id:
                    raise HTTPException(403, "Нет доступа к вложению")
            path = SUPPORT_STORAGE / attachment.storage_name
            if not path.is_file():
                raise HTTPException(404, "Файл не найден")
        from app.web.chat_photos import photo_response
        return await photo_response(path,kind=attachment.kind,mime_type=attachment.mime_type,
                                    filename=attachment.original_name,preview=preview)

    return router
