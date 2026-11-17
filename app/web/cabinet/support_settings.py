"""Owner-only quick replies and automatic support response settings."""

from fastapi import APIRouter, Cookie, HTTPException
from sqlalchemy import func, select

from app.database.models import User, WebQuickReply, WebSupportAttachment
from app.database.repositories import SQLRepository
from app.web.cabinet.context import SUPPORT_STORAGE, WebContext
from app.web.cabinet.schemas import AutoreplyPayload, QuickReplyPayload
from app.web.cabinet.support_presenter import attachment_json
from app.web.cabinet.support_storage import store_attachments
from app.web.cabinet.support_library import customer_ids, PREFIX


def support_settings_router(context: WebContext) -> APIRouter:
    database = context.database
    owner = context.owner
    router = APIRouter()

    @router.get("/api/web/admin/support/settings")
    async def support_settings(freeman_session: str | None = Cookie(default=None)):
        actor = await owner(freeman_session)
        async with database.sessions.begin() as session:
            repo = SQLRepository(session)
            await session.execute(
                select(User).where(User.id == actor.id).with_for_update()
            )
            if not await repo.setting("support_examples_v150"):
                if not await session.scalar(
                    select(func.count()).select_from(WebQuickReply)
                ):
                    session.add_all(
                        [
                            WebQuickReply(
                                title="Помощь с подключением",
                                text="Здравствуйте! Напишите, пожалуйста, модель устройства и название приложения. Если при подключении появляется ошибка, приложите её скриншот.",
                                position=10,
                            ),
                            WebQuickReply(
                                title="Проверка платежа",
                                text="Здравствуйте! Пришлите, пожалуйста, дату и сумму платежа или номер заказа. Мы проверим оплату. Полный номер карты и коды подтверждения отправлять не нужно.",
                                position=20,
                            ),
                        ]
                    )
                await repo.set_setting("support_examples_v150", "1")
                await session.flush()
            if not await repo.setting("web_customer_help_initialized"):
                for position, (title, text) in enumerate([
                    ("Не работает ключ", "Здравствуйте! У меня не работает ключ. Помогите проверить подключение. Уточню устройство и приложение."),
                    ("Не работает YouTube", "Здравствуйте! При подключении не работает YouTube. Помогите проверить настройки."),
                    ("Проблема с подключением", "Здравствуйте! Не получается подключиться. Помогите проверить ключ и настройки приложения."),
                    ("Не открывается сервис", "Здравствуйте! При подключении не открывается нужный сервис. Пришлю его название и сообщение об ошибке."),
                ]):
                    item = WebQuickReply(title=title, text=text, position=(position+1)*10)
                    session.add(item)
                    await session.flush()
                    await repo.set_setting(PREFIX+str(item.id), "customer")
                await repo.set_setting("web_customer_help_initialized", "1")
            help_ids = await customer_ids(session)
            replies = []
            for item in await session.scalars(
                select(WebQuickReply).order_by(WebQuickReply.position, WebQuickReply.id)
            ):
                assets = await session.scalars(
                    select(WebSupportAttachment).where(
                        WebSupportAttachment.quick_reply_id == item.id
                    )
                )
                replies.append(
                    dict(
                        id=item.id,
                        audience="customer" if item.id in help_ids else "operator",
                        title=item.title,
                        text=item.text,
                        enabled=item.enabled,
                        position=item.position,
                        attachments=[attachment_json(x) for x in assets],
                    )
                )
            return dict(
                quick_replies=replies,
                autoreply=dict(
                    enabled=await repo.setting("web_support_autoreply_enabled", "0")
                    == "1",
                    text=await repo.setting(
                        "web_support_autoreply_text",
                        "Спасибо! Мы получили сообщение и скоро ответим.",
                    ),
                ),
            )

    @router.put("/api/web/admin/support/quick-replies/{reply_id}")
    async def update_quick_reply(
        reply_id: int,
        payload: QuickReplyPayload,
        freeman_session: str | None = Cookie(default=None),
    ):
        await owner(freeman_session)
        obsolete = []
        async with database.sessions.begin() as session:
            item = await session.get(WebQuickReply, reply_id)
            if not item:
                raise HTTPException(404, "Быстрый ответ не найден")
            assets = list(
                await session.scalars(
                    select(WebSupportAttachment).where(
                        WebSupportAttachment.quick_reply_id == reply_id
                    )
                )
            )
            if not payload.text.strip() and not payload.attachments and not assets:
                raise HTTPException(422, "Добавьте текст или медиа")
            removed = set(payload.remove_attachment_ids)
            if not removed.issubset({asset.id for asset in assets}):
                raise HTTPException(422, "Вложение не принадлежит этому шаблону")
            remaining = [asset for asset in assets if asset.id not in removed]
            if len(remaining) + len(payload.attachments) > 6:
                raise HTTPException(422, "В шаблоне может быть до 6 файлов")
            if not payload.text.strip() and not payload.attachments and not remaining:
                raise HTTPException(422, "Добавьте текст или медиа")
            for asset in assets:
                if asset.id in removed:
                    from app.web.cabinet.support_storage import protect_asset
                    await protect_asset(session, asset.storage_name)
                    await session.delete(asset)
            await SQLRepository(session).set_setting(PREFIX+str(item.id), payload.audience)
            item.title = payload.title.strip()
            item.text = payload.text.strip()
            item.position = payload.position
            item.enabled = payload.enabled
            await store_attachments(
                session, payload.attachments, quick_reply_id=reply_id
            )
        for path in obsolete:
            pass  # Shared library files are retained for existing chats.
        return {"id": reply_id}

    @router.post("/api/web/admin/support/quick-replies")
    async def create_quick_reply(
        payload: QuickReplyPayload, freeman_session: str | None = Cookie(default=None)
    ):
        await owner(freeman_session)
        if not payload.text.strip() and not payload.attachments:
            raise HTTPException(422, "Добавьте текст или медиа")
        async with database.sessions.begin() as session:
            item = WebQuickReply(
                title=payload.title.strip(),
                text=payload.text.strip(),
                position=payload.position,
                enabled=payload.enabled,
            )
            session.add(item)
            await session.flush()
            await SQLRepository(session).set_setting(PREFIX+str(item.id), payload.audience)
            assets = await store_attachments(
                session, payload.attachments, quick_reply_id=item.id
            )
            return {
                "id": item.id,
                "title": item.title,
                "text": item.text,
                "position": item.position,
                "attachments": [attachment_json(asset) for asset in assets],
            }

    @router.delete("/api/web/admin/support/quick-replies/{reply_id}", status_code=204)
    async def delete_quick_reply(
        reply_id: int, freeman_session: str | None = Cookie(default=None)
    ):
        await owner(freeman_session)
        async with database.sessions.begin() as session:
            item = await session.get(WebQuickReply, reply_id)
            if not item:
                raise HTTPException(404, "Быстрый ответ не найден")
            assets = list(
                await session.scalars(
                    select(WebSupportAttachment).where(
                        WebSupportAttachment.quick_reply_id == item.id
                    )
                )
            )
            for asset in assets:
                from app.web.cabinet.support_storage import protect_asset
                await protect_asset(session, asset.storage_name)
                await session.delete(asset)
            await session.flush()  # Remove template assets before their parent FK.
            await session.delete(item)
            from app.database.models import AppSetting
            flag = await session.get(AppSetting, PREFIX+str(item.id))
            if flag:
                await session.delete(flag)

    @router.put("/api/web/admin/support/autoreply")
    async def set_autoreply(
        payload: AutoreplyPayload, freeman_session: str | None = Cookie(default=None)
    ):
        await owner(freeman_session)
        if payload.enabled and not payload.text.strip():
            raise HTTPException(422, "Введите текст автоответа")
        async with database.sessions.begin() as session:
            repo = SQLRepository(session)
            await repo.set_setting(
                "web_support_autoreply_enabled", "1" if payload.enabled else "0"
            )
            await repo.set_setting("web_support_autoreply_text", payload.text.strip())
            return payload.model_dump()

    return router
