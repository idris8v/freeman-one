"""One renewal policy for customer cards, quotes and payment fulfillment."""
from sqlalchemy import select
from app.database.models import AdminAudit, now
from app.services.offers import utc


async def can_renew_proxy(repo, credential):
    if credential is None:
        return False
    if credential.status in ('active', 'expired'):
        return True
    if credential.status != 'disabled' or utc(credential.expires_at) > now():
        return False
    # Telemt automatically disables expired access. Preserve explicit operator blocks.
    event = await repo.session.scalar(select(AdminAudit).where(
        AdminAudit.target == str(credential.id),
        AdminAudit.action.in_((
            'proxy_control_access_block', 'proxy_control_access_unblock',
            'proxy_control_block', 'proxy_control_unblock',
            'proxy_control_reset_enable_failed', 'proxy_key_toggle',
        )),
    ).order_by(AdminAudit.id.desc()).limit(1))
    if not event:
        return True
    return event.action in ('proxy_control_access_unblock', 'proxy_control_unblock') or (
        event.action == 'proxy_key_toggle' and event.details == 'active'
    )
