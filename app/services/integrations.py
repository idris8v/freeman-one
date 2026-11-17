from typing import Protocol


class PaymentService(Protocol):
    async def checkout(self, user_id: int, amount: int) -> str: ...


class ProvisioningService(Protocol):
    async def connection(self, subscription_id: int) -> str: ...
    async def add_device(self, subscription_id: int) -> str: ...


class NotificationService(Protocol):
    async def enqueue(self, user_id: int, text: str) -> None: ...


class MockPaymentService:
    async def checkout(self, user_id, amount):
        return 'Онлайн-оплата будет подключена на следующем этапе.'


class MockProvisioningService:
    async def connection(self, subscription_id):
        return 'Данные подключения будут доступны после подключения серверной инфраструктуры.'

    async def add_device(self, subscription_id):
        return 'Функция будет доступна после подключения серверной инфраструктуры.'


class QueuedNotificationService:
    def __init__(self, repo):
        self.repo = repo

    async def enqueue(self, user_id, text):
        from app.database.models import Notification
        await self.repo.add(Notification(user_id=user_id, text=text))
