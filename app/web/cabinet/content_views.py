"""Useful-link and news response mapping and target validation."""

from datetime import timezone
from urllib.parse import urlparse

from fastapi import HTTPException


def validate_target(value):
    internal = {
        "instructions",
        "faq",
        "status",
        "support",
        "subscriptions",
        "keys",
        "proxy",
        "balance",
    }
    if value in internal or not value:
        return value
    parsed = urlparse(value)
    if (
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
    ):
        return value
    raise HTTPException(422, "Используйте раздел сайта или публичную HTTPS-ссылку")


def link_json(item):
    return {
        "id": item.id,
        "label": item.label,
        "target": item.target,
        "icon": item.icon,
        "position": item.position,
        "enabled": item.enabled,
    }


def news_json(item):
    return {
        "id": item.id,
        "title": item.title,
        "text": item.text,
        "target": item.target,
        "category": item.category,
        "published": item.published,
        "created_at": item.created_at.replace(tzinfo=timezone.utc).isoformat(),
    }
