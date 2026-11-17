"""Support attachment validation and filesystem persistence."""

import base64
import binascii
import secrets
from pathlib import Path

from fastapi import HTTPException

from app.database.models import WebSupportAttachment
from app.web.cabinet.context import SUPPORT_STORAGE


def safe_attachment(payload):
    allowed = {
        "image/jpeg": ("image", ".jpg"),
        "image/png": ("image", ".png"),
        "image/webp": ("image", ".webp"),
        "image/gif": ("image", ".gif"),
        "video/mp4": ("video", ".mp4"),
        "video/webm": ("video", ".webm"),
        "video/quicktime": ("video", ".mov"),
    }
    if payload.mime_type not in allowed:
        raise HTTPException(
            422, "Разрешены только изображения JPG, PNG, WebP, GIF и видео MP4, WebM, MOV. Для речи используйте голосовой ввод в текст."
        )
    try:
        raw = base64.b64decode(payload.data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "Вложение повреждено")
    if not raw or len(raw) > 20 * 1024 * 1024:
        raise HTTPException(422, "Размер каждого вложения должен быть до 20 МБ")
    return allowed[payload.mime_type][0], allowed[payload.mime_type][1], raw


async def store_attachments(session, payloads, message_id=None, quick_reply_id=None):
    if not payloads:
        return []
    SUPPORT_STORAGE.mkdir(parents=True, exist_ok=True)
    saved = []
    written = []
    try:
        for payload in payloads:
            kind, suffix, raw = safe_attachment(payload)
            storage_name = secrets.token_hex(24) + suffix
            mime=payload.mime_type
            if kind=='image':
                from app.web.chat_photos import compact_upload
                raw,mime,suffix=await compact_upload(raw,mime,suffix)
                storage_name=storage_name.rsplit('.',1)[0]+suffix
            path = SUPPORT_STORAGE / storage_name
            path.write_bytes(raw)
            written.append(path)
            item = WebSupportAttachment(
                message_id=message_id,
                quick_reply_id=quick_reply_id,
                kind=kind,
                mime_type=mime,
                original_name=(Path(payload.name).stem+suffix if mime!=payload.mime_type else Path(payload.name).name)[:255],
                storage_name=storage_name,
                size=len(raw),
            )
            session.add(item)
            await session.flush()
            if quick_reply_id is not None:
                await protect_asset(session,storage_name)
            saved.append(item)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return saved


async def protect_asset(session, storage_name):
    """Approved library media is durable, including historical AI video actions."""
    from app.database.repositories import SQLRepository
    if Path(storage_name).name != storage_name or not storage_name:
        raise ValueError('unsafe_shared_media')
    await SQLRepository(session).set_setting('support_shared_media:'+storage_name,'1')


async def reference_asset(session, asset, message_id):
    """Keep per-message authorization records, reuse the immutable library file."""
    name=asset.storage_name
    if Path(name).name != name or not name:
        raise ValueError('unsafe_shared_media')
    path=SUPPORT_STORAGE/name
    if path.is_symlink() or not path.is_file():
        raise ValueError('shared_media_missing')
    await protect_asset(session,name)
    from app.database.repositories import SQLRepository
    reference="ref-"+secrets.token_hex(24)+Path(name).suffix
    import os
    os.link(path,SUPPORT_STORAGE/reference)
    item=WebSupportAttachment(message_id=message_id,kind=asset.kind,mime_type=asset.mime_type,
        original_name=asset.original_name,storage_name=reference,size=asset.size)
    try:
        session.add(item)
        await session.flush()
    except BaseException:
        (SUPPORT_STORAGE/reference).unlink(missing_ok=True)
        raise
    return item
