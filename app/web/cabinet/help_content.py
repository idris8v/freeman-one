"""Help uses the existing publication editor, storage and version checks."""
from sqlalchemy import select
from app.database.models import AppSetting, Publication, WebNews
from app.database.repositories import SQLRepository

SECTIONS = ("faq", "instructions")
ORIGIN = "support_help_origin:"
REPLACED = "support_help_replaced:"


def defaults():
    from app.config.content import FAQ, INSTRUCTIONS
    return [dict(help_key=f"faq:{i}", section="faq", title=q, text=a,
                 title_setting=f"faq_question:{i}", text_setting=f"faq:{i}")
            for i, (q, a) in enumerate(FAQ)] + [
        dict(help_key=f"instructions:{key}", section="instructions", title=title, text=text,
             title_setting=f"instruction_title:{key}", text_setting=f"instruction:{key}")
        for key, (title, text) in INSTRUCTIONS.items()]


async def legacy_items(session, include_hidden=False):
    repo = SQLRepository(session)
    result = []
    for item in defaults():
        flag = await repo.setting(REPLACED + item["help_key"])
        if flag and not (include_hidden and flag == "hidden"):
            continue
        result.append(dict(help_key=item["help_key"], section=item["section"], enabled=flag!="hidden",
                           title=await repo.setting(item["title_setting"], item["title"]),
                           text=await repo.setting(item["text_setting"], item["text"])))
    return result


async def client_help(session):
    result = {"faq": [], "instructions": []}
    for item in await legacy_items(session):
        if item["section"] == "faq":
            result["faq"].append(dict(question=item["title"], answer=item["text"]))
        else:
            result["instructions"].append(dict(id=item["help_key"], title=item["title"], text=item["text"]))
    rows = await session.execute(select(Publication, WebNews).join(WebNews, Publication.id == WebNews.id)
        .where(Publication.section.in_(SECTIONS), Publication.status == "published")
        .order_by(Publication.pinned.desc(), Publication.id))
    for post, news in rows:
        item = dict(id=post.id, title=news.title, text=post.plain, post_id=post.id)
        if post.section == "faq":
            item.update(question=news.title, answer=post.plain)
        result[post.section].append(item)
    return result


async def remember_origin(session, post, help_key):
    """Explicit edits only; opening the library never creates records."""
    if not help_key:
        return
    valid = {item["help_key"]: item["section"] for item in defaults()}
    if valid.get(help_key) != post.section:
        raise ValueError("Исходная инструкция не соответствует разделу")
    repo = SQLRepository(session)
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy import text
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": REPLACED + help_key})
    previous = await repo.setting(ORIGIN + str(post.id))
    if previous and previous != help_key:
        raise ValueError("Исходную запись нельзя заменить")
    existing = await repo.setting(REPLACED + help_key)
    if existing and existing != "hidden" and existing != str(post.id):
        raise ValueError("Эта запись уже перенесена в редактор. Обновите список.")
    await repo.set_setting(ORIGIN + str(post.id), help_key)


async def replace_legacy_on_publish(session, post):
    if post.section not in SECTIONS or post.status != "published":
        return
    repo = SQLRepository(session)
    key = await repo.setting(ORIGIN + str(post.id))
    if key:
        try:
            await remember_origin(session, post, key)
        except ValueError as error:
            from fastapi import HTTPException
            raise HTTPException(409, str(error)) from error
        await repo.set_setting(REPLACED + key, str(post.id))
