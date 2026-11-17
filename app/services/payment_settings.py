"""Encrypted payment provider configuration managed from the owner-only bot UI."""
from cryptography.fernet import Fernet, InvalidToken


FIELDS = (
    'yookassa_shop_id', 'yookassa_secret_key', 'yookassa_test_mode',
    'robokassa_merchant_login', 'robokassa_password1', 'robokassa_password2',
    'robokassa_result_url',
)


class PaymentSettings:
    def __init__(self, repo, settings):
        self.repo, self.settings = repo, settings

    def cipher(self):
        if not self.settings.encryption_key:
            raise ValueError('Для настройки платёжных систем задайте ENCRYPTION_KEY на сервере бота.')
        return Fernet(self.settings.encryption_key.encode())

    async def save(self, values):
        cipher = self.cipher()
        for key, value in values.items():
            if key not in FIELDS:
                raise ValueError('Неизвестное поле платёжной системы.')
            setattr(self.settings, key, value)
            encrypted = cipher.encrypt(str(value).encode()).decode()
            await self.repo.set_setting(f'payment_secret:{key}', encrypted)

    async def hydrate(self):
        if not self.settings.encryption_key:
            return
        cipher = self.cipher()
        for key in FIELDS:
            encrypted = await self.repo.setting(f'payment_secret:{key}')
            if not encrypted:
                continue
            try:
                value = cipher.decrypt(encrypted.encode()).decode()
            except (InvalidToken, ValueError):
                continue
            setattr(self.settings, key, value.lower() in ('1', 'true', 'yes') if key == 'yookassa_test_mode' else value)
