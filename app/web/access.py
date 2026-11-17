"""Website business administration requires the full administrator role.

Support and issuance managers are staff too, but their access is restricted
to the dedicated support and bot issuance workflows defined in Settings.
"""
from fastapi import HTTPException


def require_business_admin(actor, settings):
    if not settings.is_owner(actor.telegram_id):
        raise HTTPException(403, 'Нет прав на управление сервисом')
    return actor
