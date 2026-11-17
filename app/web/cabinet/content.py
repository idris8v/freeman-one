"""Public documents, authenticated help and owner content administration."""

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.database.models import WebNews, WebUsefulLink
from app.database.repositories import SQLRepository
from app.web.cabinet.content_views import link_json, news_json, validate_target
from app.web.cabinet.context import STATIC, WebContext
from app.web.cabinet.schemas import LinkPayload, NewsPayload


def content_router(context: WebContext) -> APIRouter:
    database = context.database
    current_user = context.current_user
    owner = context.owner
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html")

    @router.get("/documents", include_in_schema=False)
    @router.get("/documents/{index}", include_in_schema=False)
    async def public_documents(index: int | None = None):
        from html import escape

        from fastapi.responses import HTMLResponse

        from app.config.content import DOCUMENT_TEXTS, DOCUMENTS

        if index is not None and not 0 <= index < len(DOCUMENTS):
            raise HTTPException(404, "Документ не найден")
        async with database.sessions() as session:
            repo = SQLRepository(session)
            items = []
            for n, title in enumerate(DOCUMENTS):
                title = await repo.setting(f"document_title:{n}") or title
                body = await repo.setting(f"document:{n}") or DOCUMENT_TEXTS[n]
                if index is None:
                    items.append(f'<p><a href="/documents/{n}">{escape(title)}</a></p>')
                elif index == n:
                    items.append(
                        "<h1>"
                        + escape(title)
                        + "</h1>"
                        + "".join(
                            "<p>" + escape(x).replace("\n", "<br>") + "</p>"
                            for x in body.split("\n\n")
                        )
                    )
        content = "".join(items)
        return HTMLResponse(
            '<!doctype html><html lang="ru" data-ui-bundle="account"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" type="image/png" sizes="64x64" href="/assets/favicon-rounded-20261029.png"><title>Документы — Freeman ONE</title><script src="/assets/theme-init.js?v=0914e88d7a32"></script><link rel="stylesheet" href="/assets/account-management.css?v=5f7d3d8071a8"></head><body><div class="shell"><header><a class="brand" href="/"><img src="/assets/freeman-mark-20261029.png" alt="">Freeman ONE</a><a class="fo-button fo-scope" href="/documents">Документы</a></header><main class="fo-card fo-scope fo-reading">'
            + content
            + "</main></div></body></html>"
        )

    @router.get("/api/web/faq")
    async def faq(freeman_session: str | None = Cookie(default=None)):
        await current_user(freeman_session)
        from app.web.cabinet.help_content import client_help
        async with database.sessions() as session:
            return await client_help(session)

    @router.get("/api/web/admin/help/legacy")
    async def help_legacy(freeman_session: str | None = Cookie(default=None)):
        await owner(freeman_session)
        from app.web.cabinet.help_content import legacy_items
        async with database.sessions() as session:
            return {"items": await legacy_items(session, include_hidden=True)}

    @router.post("/api/web/admin/help/legacy")
    async def help_legacy_visibility(payload: dict, freeman_session: str | None = Cookie(default=None)):
        await owner(freeman_session)
        from app.web.cabinet.help_content import defaults, REPLACED
        key=payload.get("help_key")
        if key not in {item["help_key"] for item in defaults()} or type(payload.get("enabled")) is not bool:
            raise HTTPException(422, "Проверьте запись и состояние")
        async with database.sessions.begin() as session:
            if session.bind.dialect.name=="postgresql":
                from sqlalchemy import text
                await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": REPLACED + key})
            repo=SQLRepository(session)
            value=await repo.setting(REPLACED+key)
            if value and value!="hidden":raise HTTPException(409, "Материал уже перенесён в редактор. Обновите список.")
            await repo.set_setting(REPLACED+key,"" if payload["enabled"] else "hidden")
            return {"ok": True}

    @router.get("/api/web/admin/content")
    async def admin_content(freeman_session: str | None = Cookie(default=None)):
        await owner(freeman_session)
        async with database.sessions() as session:
            links = list(
                await session.scalars(
                    select(WebUsefulLink).order_by(
                        WebUsefulLink.position, WebUsefulLink.id
                    )
                )
            )
            news = list(
                await session.scalars(select(WebNews).order_by(WebNews.id.desc()))
            )
            return {
                "links": [link_json(item) for item in links],
                "news": [news_json(item) for item in news],
            }

    @router.post("/api/web/admin/links")
    async def create_link(
        payload: LinkPayload, freeman_session: str | None = Cookie(default=None)
    ):
        await owner(freeman_session)
        payload.target = validate_target(payload.target)
        async with database.sessions.begin() as session:
            item = WebUsefulLink(**payload.model_dump())
            session.add(item)
            await session.flush()
            return link_json(item)

    @router.put("/api/web/admin/links/{item_id}")
    async def update_link(
        item_id: int,
        payload: LinkPayload,
        freeman_session: str | None = Cookie(default=None),
    ):
        await owner(freeman_session)
        payload.target = validate_target(payload.target)
        async with database.sessions.begin() as session:
            item = await session.get(WebUsefulLink, item_id)
            if not item:
                raise HTTPException(404, "Ссылка не найдена")
            for key, value in payload.model_dump().items():
                setattr(item, key, value)
            return link_json(item)

    @router.delete("/api/web/admin/links/{item_id}", status_code=204)
    async def delete_link(
        item_id: int, freeman_session: str | None = Cookie(default=None)
    ):
        await owner(freeman_session)
        async with database.sessions.begin() as session:
            item = await session.get(WebUsefulLink, item_id)
            if not item:
                raise HTTPException(404, "Ссылка не найдена")
            await session.delete(item)

    @router.post("/api/web/admin/news")
    async def create_news(
        payload: NewsPayload, freeman_session: str | None = Cookie(default=None)
    ):
        await owner(freeman_session)
        payload.target = validate_target(payload.target)
        async with database.sessions.begin() as session:
            item = WebNews(**payload.model_dump())
            session.add(item)
            await session.flush()
            return news_json(item)

    @router.put("/api/web/admin/news/{item_id}")
    async def update_news(
        item_id: int,
        payload: NewsPayload,
        freeman_session: str | None = Cookie(default=None),
    ):
        await owner(freeman_session)
        payload.target = validate_target(payload.target)
        async with database.sessions.begin() as session:
            item = await session.get(WebNews, item_id)
            if not item:
                raise HTTPException(404, "Новость не найдена")
            for key, value in payload.model_dump().items():
                setattr(item, key, value)
            return news_json(item)

    @router.delete("/api/web/admin/news/{item_id}", status_code=204)
    async def delete_news(
        item_id: int, freeman_session: str | None = Cookie(default=None)
    ):
        await owner(freeman_session)
        async with database.sessions.begin() as session:
            item = await session.get(WebNews, item_id)
            if not item:
                raise HTTPException(404, "Новость не найдена")
            await session.delete(item)

    return router
