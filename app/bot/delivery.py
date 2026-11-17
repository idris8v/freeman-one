"""Recover navigation delivery without rerunning the business operation."""
from datetime import datetime, timezone
from aiogram.types import Message


def edit_unavailable(error):
    text = str(error).lower()
    return any(reason in text for reason in (
        "message can't be edited", 'message cannot be edited',
        'message to edit not found', 'there is no text in the message to edit',
        'there is no caption in the message to edit', 'message caption is too long',
    ))


def fresh_context(query):
    # A callback may contain InaccessibleMessage (date=0), without media fields.
    # Use the authenticated callback's chat/sender, not stale content or IDs in data.
    return Message(message_id=query.message.message_id, date=datetime.now(timezone.utc),
        chat=query.message.chat, from_user=query.from_user).as_(query.bot)
