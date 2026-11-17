from datetime import timezone
from sqlalchemy import select, update
from app.database.models import AccessKey, User, AdminAudit, now


class KeyService:
    def __init__(self, repo, settings):
        self.repo, self.settings = repo, settings

    def require_admin(self, actor):
        if not self.settings.is_admin(actor.telegram_id):
            raise PermissionError('Admin access denied')

    async def owned(self, user_id, key_id):
        return await self.repo.session.scalar(select(AccessKey).where(AccessKey.id == key_id, AccessKey.user_id == user_id, AccessKey.status != 'deleted'))

    async def preview_assignment(self, actor, key_id, customer_id):
        self.require_admin(actor)
        customer = await self.repo.session.get(User, customer_id)
        key = await self.repo.session.get(AccessKey, key_id)
        if not customer or customer.status == 'blocked':
            raise ValueError('Клиент не найден или заблокирован.')
        if not key or key.status == 'deleted':
            raise ValueError('Ключ не найден или удалён.')
        expiry = key.expires_at.replace(tzinfo=timezone.utc) if key.expires_at.tzinfo is None else key.expires_at
        if expiry <= now():
            raise ValueError('Срок ключа истёк. Выберите другой ключ.')
        if key.mode != 'mock':
            raise ValueError('Передача реальных ключей будет доступна после подключения серверной части.')
        return key, customer

    async def assign(self, actor, key_id, customer_id, expected_owner, expected_status):
        self.require_admin(actor)
        key, customer = await self.preview_assignment(actor, key_id, customer_id)
        if key.user_id == customer.id:
            raise ValueError('Ключ уже назначен этому клиенту.')
        result = await self.repo.session.execute(update(AccessKey).where(
            AccessKey.id == key_id, AccessKey.user_id == expected_owner,
            AccessKey.status == expected_status, AccessKey.status != 'deleted',
            AccessKey.mode == 'mock', AccessKey.expires_at > now(),
        ).values(user_id=customer.id).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise ValueError('Ключ изменился. Выберите его заново и подтвердите передачу.')
        previous = await self.repo.session.get(User, expected_owner)
        await self.repo.add(AdminAudit(actor_telegram_id=actor.telegram_id, action='key_assign', target=str(key_id),
            details=f'Владелец {previous.telegram_id if previous else expected_owner} → {customer.telegram_id}; статус {expected_status} сохранён'))
        await self.repo.session.refresh(key)
        return key
